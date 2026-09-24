import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  add,
  commit,
  formatCommitMessage,
  GitError,
  git,
  isAncestor,
  log,
  revParse,
  show,
} from "./git.ts";
import { type TempRepo, tempRepo } from "./testing/git.ts";

const NODE_A = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44";
const NODE_B = "0192f2c8-1a0d-7e33-8c41-5b2a9d0e7f10";

let repo: TempRepo;
beforeEach(async () => {
  repo = await tempRepo();
});
afterEach(() => repo.cleanup());

describe("formatCommitMessage", () => {
  it("puts trailers in the last paragraph", () => {
    const text = formatCommitMessage({
      subject: "decompose 1c0b9a44: 2 children",
      body: "Split the streaming work.",
      trailers: [
        ["APM-Op", "decompose"],
        ["APM-Nodes", `${NODE_A},${NODE_B}`],
      ],
    });
    expect(text).toBe(
      `decompose 1c0b9a44: 2 children\n\nSplit the streaming work.\n\nAPM-Op: decompose\nAPM-Nodes: ${NODE_A},${NODE_B}\n`,
    );
    expect(formatCommitMessage({ subject: "s" })).toBe("s\n");
  });

  it("rejects values that would corrupt the message", () => {
    expect(() => formatCommitMessage({ subject: "" })).toThrow(/subject/);
    expect(() => formatCommitMessage({ subject: "a\nb" })).toThrow(/subject/);
    expect(() => formatCommitMessage({ subject: "s", trailers: [["Bad Key", "v"]] })).toThrow(
      /trailer key/,
    );
    expect(() => formatCommitMessage({ subject: "s", trailers: [["K", "a\nK2: b"]] })).toThrow(
      /one non-blank line/,
    );
  });
});

describe("git wrapper", () => {
  it("commits with trailers that round-trip through log", async () => {
    await writeFile(join(repo.dir, "graph.ndjson"), "");
    await add(repo.dir, ["graph.ndjson"]);
    const sha = await commit(repo.dir, {
      subject: "create 1c0b9a44: Stream agent output",
      trailers: [
        ["APM-Op", "create"],
        ["APM-Nodes", NODE_A],
        ["APM-Actor", "agent:apm-decomposer"],
      ],
    });
    expect(sha).toMatch(/^[0-9a-f]{40}$/);
    const [entry] = await log(repo.dir);
    expect(entry).toMatchObject({
      sha,
      subject: "create 1c0b9a44: Stream agent output",
      trailers: { "APM-Op": "create", "APM-Nodes": NODE_A, "APM-Actor": "agent:apm-decomposer" },
    });
    expect(entry?.time).toBeGreaterThan(1_700_000_000);
  });

  it("keeps a subject starting with #", async () => {
    await repo.commitFile("a", "1", "#12: keep me");
    expect((await log(repo.dir))[0]?.subject).toBe("#12: keep me");
  });

  it("handles an empty repo", async () => {
    expect(await revParse(repo.dir, "HEAD")).toBeUndefined();
    expect(await log(repo.dir)).toEqual([]);
  });

  it("logs newest first, or oldest first, filtered by path and range", async () => {
    const first = await repo.commitFile("graph.ndjson", "1\n");
    const second = await repo.commitFile("other", "x");
    const third = await repo.commitFile("graph.ndjson", "2\n");
    const shas = async (o: Parameters<typeof log>[1]) => (await log(repo.dir, o)).map((e) => e.sha);
    expect(await shas({})).toEqual([third, second, first]);
    expect(await shas({ reverse: true, paths: ["graph.ndjson"] })).toEqual([first, third]);
    expect(await shas({ range: `${first}..HEAD` })).toEqual([third, second]);
  });

  it("reads files at a commit with show", async () => {
    const first = await repo.commitFile("graph.ndjson", "old\n");
    await repo.commitFile("graph.ndjson", "new\n");
    expect(await show(repo.dir, first, "graph.ndjson")).toBe("old\n");
    expect(await show(repo.dir, "HEAD", "graph.ndjson")).toBe("new\n");
    await expect(show(repo.dir, "HEAD", "missing")).rejects.toBeInstanceOf(GitError);
  });

  it("resolves revisions and tests ancestry", async () => {
    const first = await repo.commitFile("a", "1");
    const second = await repo.commitFile("a", "2");
    expect(await revParse(repo.dir, "HEAD")).toBe(second);
    expect(await revParse(repo.dir, "HEAD~1")).toBe(first);
    expect(await revParse(repo.dir, "no-such-branch")).toBeUndefined();
    expect(await isAncestor(repo.dir, first, second)).toBe(true);
    expect(await isAncestor(repo.dir, second, first)).toBe(false);
    expect(await isAncestor(repo.dir, second, second)).toBe(true);

    // A rewritten history: the old HEAD is no longer an ancestor of the new one.
    await git(repo.dir, ["reset", "--quiet", "--hard", first]);
    const rewritten = await repo.commitFile("a", "3");
    expect(await isAncestor(repo.dir, second, rewritten)).toBe(false);
  });

  it("reports git's stderr and exit code in GitError", async () => {
    const err = await git(repo.dir, ["merge-base", "--is-ancestor", "nope", "HEAD"]).catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(GitError);
    expect((err as GitError).exitCode).toBe(128);
    expect((err as GitError).stderr).toMatch(/nope/);
    await expect(isAncestor(repo.dir, "nope", "HEAD")).rejects.toBeInstanceOf(GitError);
  });
});
