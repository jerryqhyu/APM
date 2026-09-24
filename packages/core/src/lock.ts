// .cache/write.lock (plans.md §6.2): one writer at a time, across processes.
//
// The lock file is created with O_EXCL and records its owner. It's stale if its owner's PID is
// dead on this host. Breaking a stale lock happens under a second O_EXCL file, `write.lock.break`,
// so two processes can't both judge the same lock stale and one delete the other's new lock.

import { randomBytes } from "node:crypto";
import { mkdir, open, readFile, stat, unlink } from "node:fs/promises";
import { hostname } from "node:os";
import { join } from "node:path";

export const CACHE_DIR = ".cache";
export const LOCK_FILE = "write.lock";

export function lockPath(apmDir: string): string {
  return join(apmDir, CACHE_DIR, LOCK_FILE);
}

export interface LockOwner {
  pid: number;
  hostname: string;
  /** ISO timestamp. */
  acquired: string;
  /** Random, so an owner can tell its lock from a later one. */
  token: string;
}

export class LockTimeoutError extends Error {
  override name = "LockTimeoutError";
  /** Who held the lock when we gave up, if the file was readable. */
  readonly owner: LockOwner | undefined;

  constructor(path: string, timeoutMs: number, owner: LockOwner | undefined) {
    const by = owner ? ` by pid ${owner.pid} on ${owner.hostname} since ${owner.acquired}` : "";
    super(`timed out after ${timeoutMs}ms waiting for ${path}, held${by}`);
    this.owner = owner;
  }
}

export interface LockOptions {
  /** Give up after this long. Default 10s. */
  timeoutMs?: number;
  /** Wait between attempts. Default 25ms. */
  retryMs?: number;
}

export interface WriteLock {
  readonly owner: LockOwner;
  /** Removes the lock if it's still ours. Safe to call twice. */
  release(): Promise<void>;
}

// A lock file that can't be parsed is normally mid-write; after this long, it's abandoned.
const UNREADABLE_GRACE_MS = 2_000;
// A break file left by a process that died mid-break.
const BREAK_GRACE_MS = 5_000;

export async function acquireWriteLock(
  apmDir: string,
  options: LockOptions = {},
): Promise<WriteLock> {
  const { timeoutMs = 10_000, retryMs = 25 } = options;
  const path = lockPath(apmDir);
  await mkdir(join(apmDir, CACHE_DIR), { recursive: true });
  const owner: LockOwner = {
    pid: process.pid,
    hostname: hostname(),
    acquired: new Date().toISOString(),
    token: randomBytes(8).toString("hex"),
  };
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await createExclusive(path, JSON.stringify(owner))) {
      let released = false;
      return {
        owner,
        async release() {
          if (released) return;
          released = true;
          if ((await readOwner(path))?.token === owner.token)
            await unlink(path).catch(ignoreMissing);
        },
      };
    }
    await breakIfStale(path);
    if (Date.now() >= deadline) throw new LockTimeoutError(path, timeoutMs, await readOwner(path));
    await new Promise((r) => setTimeout(r, retryMs));
  }
}

/** Runs `fn` holding the write lock, and releases it however `fn` ends. */
export async function withWriteLock<T>(
  apmDir: string,
  fn: () => Promise<T>,
  options?: LockOptions,
): Promise<T> {
  const lock = await acquireWriteLock(apmDir, options);
  try {
    return await fn();
  } finally {
    await lock.release();
  }
}

async function breakIfStale(path: string): Promise<void> {
  const breakPath = `${path}.break`;
  if (!(await createExclusive(breakPath, String(process.pid)))) {
    if ((await ageMs(breakPath)) > BREAK_GRACE_MS) await unlink(breakPath).catch(ignoreMissing);
    return;
  }
  try {
    // Re-checked under the break file: only a breaker ever deletes someone else's lock.
    if (await isStale(path)) await unlink(path).catch(ignoreMissing);
  } finally {
    await unlink(breakPath).catch(ignoreMissing);
  }
}

async function isStale(path: string): Promise<boolean> {
  const owner = await readOwner(path);
  if (!owner) return (await ageMs(path)) > UNREADABLE_GRACE_MS;
  // A PID on another host can't be checked, so its lock is never judged stale.
  return owner.hostname === hostname() && !isAlive(owner.pid);
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "EPERM";
  }
}

async function createExclusive(path: string, content: string): Promise<boolean> {
  try {
    const handle = await open(path, "wx");
    try {
      await handle.writeFile(content);
    } finally {
      await handle.close();
    }
    return true;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "EEXIST") return false;
    throw err;
  }
}

async function readOwner(path: string): Promise<LockOwner | undefined> {
  try {
    const owner = JSON.parse(await readFile(path, "utf8")) as Partial<LockOwner>;
    return typeof owner.pid === "number" && typeof owner.token === "string"
      ? (owner as LockOwner)
      : undefined;
  } catch {
    return undefined;
  }
}

/** Age from mtime; 0 if the file is gone, so a vanished file is never "old". */
async function ageMs(path: string): Promise<number> {
  try {
    return Date.now() - (await stat(path)).mtimeMs;
  } catch {
    return 0;
  }
}

function ignoreMissing(err: NodeJS.ErrnoException): void {
  if (err.code !== "ENOENT") throw err;
}
