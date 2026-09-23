import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { Node } from "./model.ts";
import { GraphParseError, parseGraph } from "./ndjson.ts";

const fixture = (name: string) =>
  readFileSync(new URL(`../fixtures/${name}`, import.meta.url), "utf8");

const ROOT = "0192f2c8-1a0d-7e33-8c41-5b2a9d0e7f10";
const MODEL = "0192e9d0-55aa-7c12-9f00-aa11bb22cc33";
const STREAM = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44";
const LAYOUT = "01930a00-0000-7000-8000-00000000d0d0";

// The graph in fixtures/graph.golden.ndjson, in id order.
const golden: Node[] = [
  {
    id: MODEL,
    parent: ROOT,
    title: "Graph model and file I/O",
    kind: "work",
    status: "done",
    depends_on: [],
  },
  { id: ROOT, parent: null, title: "APM v1", kind: "work", status: "in_progress", depends_on: [] },
  {
    id: STREAM,
    parent: ROOT,
    title: "Stream agent output to the UI",
    kind: "work",
    status: "in_progress",
    depends_on: [MODEL],
    delivery: {
      repo: "app",
      branch: "apm/1c0b9a44-stream-output",
      commits: ["a1b2c3d", "e4f5a6b7c8d9"],
      pr: "https://github.com/jerry/app/pull/12",
    },
  },
  {
    id: LAYOUT,
    parent: ROOT,
    title: 'Pick a layout engine — "elkjs" vs dagre',
    kind: "decision",
    status: "dropped",
    depends_on: [],
    external: {
      provider: "github",
      id: "42",
      synced_at: "2026-09-20T12:00:00Z",
      synced_hash: "abc123",
    },
    variant_of: STREAM,
  },
];

describe("golden files", () => {
  it("parses the golden file", () => {
    expect(parseGraph(fixture("graph.golden.ndjson"))).toEqual(golden);
  });

  it("canonicalizes hand-written input: order, CRLF, blanks, nulls, defaults, duplicate deps", () => {
    const text = fixture("graph.handwritten.ndjson");
    expect(text).toContain("\r\n");
    expect(parseGraph(text)).toEqual(golden);
  });

  it("ignores a leading byte-order mark", () => {
    const text = fixture("graph.golden.ndjson");
    expect(parseGraph(`\uFEFF${text}`)).toEqual(golden);
    expect(parseGraph(`\uFEFF${fixture("graph.handwritten.ndjson")}`)).toEqual(golden);
  });

  it("treats an empty file as an empty graph", () => {
    expect(parseGraph("")).toEqual([]);
  });
});

describe("parse errors", () => {
  const errorsFor = (text: string, source?: string) => {
    try {
      parseGraph(text, source);
    } catch (err) {
      expect(err).toBeInstanceOf(GraphParseError);
      return (err as GraphParseError).issues.map((i) => `${i.line}: ${i.message}`);
    }
    throw new Error("expected a GraphParseError");
  };
  const ok = `{"id":"${ROOT}","title":"ok"}`;

  it("reports every bad line with its line number", () => {
    const text = [
      ok,
      "{not json",
      `{"id":"${MODEL}","title":"x","status":"blocked"}`,
      `{"id":"${STREAM}","title":"x","colour":"red"}`,
      "",
      `{"id":"${ROOT}","title":"dup"}`,
    ].join("\n");
    const issues = errorsFor(text);
    expect(issues).toHaveLength(4);
    expect(issues[0]).toMatch(/^2: invalid JSON/);
    expect(issues[1]).toMatch(/^3: status: /);
    expect(issues[2]).toMatch(/^4: .*colour/);
    expect(issues[3]).toBe(`6: duplicate id ${ROOT} (first on line 1)`);
  });

  it.each([
    [
      "an uppercase id",
      `{"id":"${ROOT.toUpperCase()}","title":"x"}`,
      /id: must be a lowercase UUIDv7/,
    ],
    ["a v4 id", `{"id":"0192f2c8-1a0d-4e33-8c41-5b2a9d0e7f10","title":"x"}`, /id: must be/],
    ["a bad dependency", `{"id":"${ROOT}","title":"x","depends_on":["nope"]}`, /depends_on\[0\]: /],
    ["a blank title", `{"id":"${ROOT}","title":"  "}`, /title: must not be blank/],
    ["a multi-line title", `{"id":"${ROOT}","title":"a\\nb"}`, /title: must be a single line/],
    ["a missing title", `{"id":"${ROOT}"}`, /title: /],
    ["a non-object", "[1,2]", /expected object/],
    [
      "a bad commit",
      `{"id":"${ROOT}","title":"x","delivery":{"repo":"app","commits":["zz"]}}`,
      /delivery\.commits\[0\]: must be a hex commit SHA/,
    ],
    [
      "a delivery without repo",
      `{"id":"${ROOT}","title":"x","delivery":{"branch":"b"}}`,
      /delivery\.repo: /,
    ],
  ])("rejects %s", (_name, line, pattern) => {
    const issues = errorsFor(line);
    expect(issues.some((i) => pattern.test(i))).toBe(true);
  });

  it("names the source file in the message", () => {
    expect(() => parseGraph("{", "plan/graph.ndjson")).toThrow(
      /^plan\/graph\.ndjson:1: invalid JSON/,
    );
  });
});
