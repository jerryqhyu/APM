import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readBody, renderBody, writeBody } from "./body.ts";
import { bodyPath, readGraphFile, writeFileAtomic, writeGraphFile } from "./files.ts";
import type { Node } from "./model.ts";

const ID = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44";

let dir: string;
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "apm-files-"));
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

describe("graph file", () => {
  it("round-trips through disk", async () => {
    const nodes: Node[] = [
      { id: ID, parent: null, title: "t", kind: "work", status: "todo", depends_on: [] },
    ];
    await writeGraphFile(dir, nodes);
    expect(await readGraphFile(dir)).toEqual(nodes);
    expect(await readdir(dir)).toEqual(["graph.ndjson"]); // no temp files left behind
  });

  it("refuses to write an unreadable graph and leaves the existing file alone", async () => {
    const good: Node = {
      id: ID,
      parent: null,
      title: "t",
      kind: "work",
      status: "todo",
      depends_on: [],
    };
    await writeGraphFile(dir, [good]);
    const before = await readFile(join(dir, "graph.ndjson"), "utf8");
    await expect(writeGraphFile(dir, [{ ...good, title: "two\nlines" }])).rejects.toThrow(
      /title: must be a single line/,
    );
    expect(await readFile(join(dir, "graph.ndjson"), "utf8")).toBe(before);
    expect(await readGraphFile(dir)).toEqual([good]);
  });

  it("names graph.ndjson in parse errors", async () => {
    await writeFile(join(dir, "graph.ndjson"), "{\n");
    await expect(readGraphFile(dir)).rejects.toThrow(/^graph\.ndjson:1: invalid JSON/);
  });
});

describe("writeFileAtomic", () => {
  it("creates parent directories and replaces existing content", async () => {
    const path = join(dir, "a", "b", "file.txt");
    await writeFileAtomic(path, "one");
    await writeFileAtomic(path, "two");
    expect(await readFile(path, "utf8")).toBe("two");
    expect(await readdir(join(dir, "a", "b"))).toEqual(["file.txt"]);
  });
});

describe("bodies", () => {
  it("reads a missing body as empty", async () => {
    expect(await readBody(dir, ID)).toBe("");
  });

  it("round-trips a body under nodes/<id>.md", async () => {
    await writeBody(dir, ID, "## Intent\n\nhello\n");
    expect(await readBody(dir, ID)).toBe("## Intent\n\nhello\n");
    expect(await readdir(join(dir, "nodes"))).toEqual([`${ID}.md`]);
  });

  it("refuses paths from anything but a node id", () => {
    expect(() => bodyPath(dir, "../../etc/passwd")).toThrow(/not a node id/);
    expect(() => bodyPath(dir, ID.toUpperCase())).toThrow(/not a node id/);
  });
});

describe("renderBody", () => {
  it("renders the empty template", () => {
    expect(renderBody()).toBe("## Intent\n\n## Acceptance criteria\n\n## Notes\n");
  });

  it("fills intent, acceptance criteria and notes", () => {
    expect(
      renderBody({
        intent: "  Show agent output live.\n",
        acceptance: ["Events appear within 1s", " Survives reconnect "],
        notes: "See §7.1.",
      }),
    ).toBe(
      [
        "## Intent",
        "",
        "Show agent output live.",
        "",
        "## Acceptance criteria",
        "",
        "- [ ] Events appear within 1s",
        "- [ ] Survives reconnect",
        "",
        "## Notes",
        "",
        "See §7.1.",
        "",
      ].join("\n"),
    );
  });
});
