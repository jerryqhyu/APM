import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readBody, renderBody, writeBody } from "./body.ts";
import { bodyPath, writeFileAtomic } from "./files.ts";

const ID = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44";

let dir: string;
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "apm-files-"));
});
afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
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
