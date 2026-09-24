// A thin wrapper over the git CLI (plans.md §3.5). Commits go through `git` itself, so the
// user's identity, signing and hooks all apply.

import { execFile } from "node:child_process";

export class GitError extends Error {
  override name = "GitError";
  readonly args: readonly string[];
  /** The exit code, or null if git was killed or couldn't be started. */
  readonly exitCode: number | null;
  readonly stderr: string;

  constructor(args: readonly string[], exitCode: number | null, stderr: string) {
    super(
      `git ${args.join(" ")} failed${exitCode === null ? "" : ` (${exitCode})`}: ${stderr.trim()}`,
    );
    this.args = args;
    this.exitCode = exitCode;
    this.stderr = stderr;
  }
}

/** Runs `git -C <cwd> <args>` and returns stdout. Throws `GitError` on a non-zero exit. */
export function git(cwd: string, args: readonly string[], input?: string): Promise<string> {
  const argv = ["-C", cwd, ...args];
  return new Promise((resolve, reject) => {
    const child = execFile("git", argv, { maxBuffer: 256 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (!err) return resolve(stdout);
      const code = typeof err.code === "number" ? err.code : null;
      reject(new GitError(args, code, stderr || err.message));
    });
    child.stdin?.end(input ?? "");
  });
}

export async function add(cwd: string, paths: readonly string[]): Promise<void> {
  if (paths.length > 0) await git(cwd, ["add", "--", ...paths]);
}

/** Trailers in the order they're written, e.g. `[["APM-Op", "create"]]`. */
export type Trailers = ReadonlyArray<readonly [key: string, value: string]>;

export interface CommitMessage {
  subject: string;
  body?: string;
  trailers?: Trailers;
}

const TRAILER_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9-]*$/;

/** Subject, optional body, then trailers as the last paragraph, where `git log` finds them. */
export function formatCommitMessage({ subject, body, trailers = [] }: CommitMessage): string {
  if (subject.trim() === "" || /[\r\n]/.test(subject)) {
    throw new Error("commit subject must be one non-blank line");
  }
  const parts = [subject];
  if (body?.trim()) parts.push(body.trim());
  for (const [key, value] of trailers) {
    if (!TRAILER_KEY_RE.test(key)) throw new Error(`invalid trailer key: ${JSON.stringify(key)}`);
    if (value.trim() === "" || /[\r\n]/.test(value)) {
      throw new Error(`trailer ${key} must be one non-blank line`);
    }
  }
  if (trailers.length > 0) parts.push(trailers.map(([k, v]) => `${k}: ${v}`).join("\n"));
  return `${parts.join("\n\n")}\n`;
}

/** Commits what's staged and returns the new commit's SHA. */
export async function commit(cwd: string, message: CommitMessage): Promise<string> {
  // --cleanup=whitespace keeps lines starting with "#", which the default would strip.
  await git(
    cwd,
    ["commit", "--quiet", "--cleanup=whitespace", "-F", "-"],
    formatCommitMessage(message),
  );
  return (await revParse(cwd, "HEAD")) as string;
}

/** The full SHA of a commit, or `undefined` if `rev` doesn't name one (e.g. HEAD before the first commit). */
export async function revParse(cwd: string, rev: string): Promise<string | undefined> {
  try {
    return (
      await git(cwd, ["rev-parse", "--verify", "--quiet", "--end-of-options", `${rev}^{commit}`])
    ).trim();
  } catch (err) {
    if (err instanceof GitError && err.exitCode === 1) return undefined;
    throw err;
  }
}

/** A file's contents at a commit. Throws `GitError` if the file or commit doesn't exist. */
export function show(cwd: string, rev: string, path: string): Promise<string> {
  return git(cwd, ["show", `${rev}:${path}`]);
}

/** True if `ancestor` is `descendant` or one of its ancestors. */
export async function isAncestor(
  cwd: string,
  ancestor: string,
  descendant: string,
): Promise<boolean> {
  try {
    await git(cwd, ["merge-base", "--is-ancestor", ancestor, descendant]);
    return true;
  } catch (err) {
    if (err instanceof GitError && err.exitCode === 1) return false;
    throw err;
  }
}

export interface LogEntry {
  sha: string;
  /** Committer time, Unix seconds. */
  time: number;
  subject: string;
  /** Trailers by key; if a key repeats, the first value wins. */
  trailers: Record<string, string>;
}

export interface LogOptions {
  /** A revision or range, e.g. `HEAD` or `<sha>..HEAD`. Default `HEAD`. */
  range?: string;
  /** Only commits touching these paths. */
  paths?: readonly string[];
  /** Oldest first. */
  reverse?: boolean;
}

/** Commits with their trailers. An empty repo has no log: returns []. */
export async function log(cwd: string, options: LogOptions = {}): Promise<LogEntry[]> {
  const range = options.range ?? "HEAD";
  if (range === "HEAD" && (await revParse(cwd, "HEAD")) === undefined) return [];
  const args = ["log", "--format=%H%x00%ct%x00%s%x00%(trailers:only,unfold)%x1e"];
  if (options.reverse) args.push("--reverse");
  args.push("--end-of-options", range, "--", ...(options.paths ?? []));
  const out = await git(cwd, args);
  return out
    .split("\x1e")
    .map((record) => record.replace(/^\n/, ""))
    .filter((record) => record !== "")
    .map((record) => {
      const [sha = "", time = "", subject = "", trailerText = ""] = record.split("\0");
      return { sha, time: Number(time), subject, trailers: parseTrailers(trailerText) };
    });
}

function parseTrailers(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const colon = line.indexOf(":");
    if (colon <= 0) continue;
    const key = line.slice(0, colon).trim();
    if (!(key in out)) out[key] = line.slice(colon + 1).trim();
  }
  return out;
}
