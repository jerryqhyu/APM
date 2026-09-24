import { execFile, spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, utimes, writeFile } from "node:fs/promises";
import { hostname, tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  acquireWriteLock,
  CACHE_DIR,
  type LockOwner,
  LockTimeoutError,
  lockPath,
  withWriteLock,
} from "./lock.ts";

let apmDir: string;
beforeEach(async () => {
  apmDir = await mkdtemp(join(tmpdir(), "apm-lock-"));
});
afterEach(() => rm(apmDir, { recursive: true, force: true }));

const fast = { timeoutMs: 200, retryMs: 5 };

/** Plants a lock file as if another process held it. */
async function plant(owner: Partial<LockOwner> | string): Promise<void> {
  await mkdir(join(apmDir, CACHE_DIR), { recursive: true });
  const content =
    typeof owner === "string"
      ? owner
      : JSON.stringify({
          pid: process.pid,
          hostname: hostname(),
          acquired: "2026-09-24T00:00:00.000Z",
          token: "someone-else",
          ...owner,
        });
  await writeFile(lockPath(apmDir), content);
}

/** The PID of a process that has already exited. */
async function deadPid(): Promise<number> {
  const child = spawn(process.execPath, ["-e", ""]);
  await new Promise((r) => child.on("exit", r));
  return child.pid as number;
}

describe("write lock", () => {
  it("records its owner, and release removes it", async () => {
    const lock = await acquireWriteLock(apmDir);
    const onDisk = JSON.parse(await readFile(lockPath(apmDir), "utf8")) as LockOwner;
    expect(onDisk).toEqual(lock.owner);
    expect(onDisk).toMatchObject({ pid: process.pid, hostname: hostname() });
    await lock.release();
    await lock.release();
    await expect(readFile(lockPath(apmDir))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("makes a second acquirer wait until the first releases", async () => {
    const first = await acquireWriteLock(apmDir);
    const order: string[] = [];
    const second = acquireWriteLock(apmDir, { retryMs: 5 }).then((l) => {
      order.push("second acquired");
      return l;
    });
    await new Promise((r) => setTimeout(r, 50));
    order.push("first released");
    await first.release();
    await (await second).release();
    expect(order).toEqual(["first released", "second acquired"]);
  });

  it("times out on a live holder, naming it", async () => {
    await plant({ pid: process.pid });
    const err = await acquireWriteLock(apmDir, fast).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(LockTimeoutError);
    expect((err as LockTimeoutError).owner?.pid).toBe(process.pid);
    expect((err as Error).message).toMatch(new RegExp(`pid ${process.pid} on ${hostname()}`));
  });

  it("recovers a stale lock whose owner is dead", async () => {
    await plant({ pid: await deadPid() });
    const lock = await acquireWriteLock(apmDir, fast);
    expect(lock.owner.pid).toBe(process.pid);
    await lock.release();
  });

  it("never judges a lock from another host stale", async () => {
    await plant({ pid: await deadPid(), hostname: "some-other-host" });
    await expect(acquireWriteLock(apmDir, fast)).rejects.toBeInstanceOf(LockTimeoutError);
  });

  it("waits on an unreadable lock (mid-write), then recovers it once abandoned", async () => {
    await plant("");
    await expect(acquireWriteLock(apmDir, fast)).rejects.toBeInstanceOf(LockTimeoutError);
    const old = new Date(Date.now() - 10_000);
    await utimes(lockPath(apmDir), old, old);
    await (await acquireWriteLock(apmDir, fast)).release();
  });

  it("release leaves a lock that isn't ours any more", async () => {
    const lock = await acquireWriteLock(apmDir);
    await plant({ token: "a-later-owner" });
    await lock.release();
    expect(await readFile(lockPath(apmDir), "utf8")).toMatch(/a-later-owner/);
  });

  it("withWriteLock releases when fn throws", async () => {
    await expect(
      withWriteLock(apmDir, async () => {
        throw new Error("boom");
      }),
    ).rejects.toThrow("boom");
    await (await acquireWriteLock(apmDir, fast)).release();
  });

  it("is never held twice across processes, and loses no updates", async () => {
    const worker = new URL("./testing/lock-worker.ts", import.meta.url).pathname;
    const processes = 6;
    const rounds = 10;
    await Promise.all(
      Array.from({ length: processes }, () =>
        promisify(execFile)(process.execPath, [worker, apmDir, String(rounds)]),
      ),
    );
    expect(await readFile(join(apmDir, "counter"), "utf8")).toBe(String(processes * rounds));
    expect(await readFile(join(apmDir, "trace"), "utf8")).toBe("+-".repeat(processes * rounds));
  }, 60_000);
});
