import { readFileSync } from "node:fs";
import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { KINDS, type Node, STATUSES } from "./model.ts";
import { GraphParseError, InvalidNodeError, parseGraph, serializeGraph } from "./ndjson.ts";

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
  it("serializes the golden graph byte-for-byte", () => {
    expect(serializeGraph(golden)).toBe(fixture("graph.golden.ndjson"));
  });

  it("parses the golden file", () => {
    expect(parseGraph(fixture("graph.golden.ndjson"))).toEqual(golden);
  });

  it("serializes regardless of input order", () => {
    expect(serializeGraph([...golden].reverse())).toBe(fixture("graph.golden.ndjson"));
  });

  it("canonicalizes hand-written input: order, CRLF, blanks, nulls, defaults, duplicate deps", () => {
    const text = fixture("graph.handwritten.ndjson");
    expect(text).toContain("\r\n");
    expect(parseGraph(text)).toEqual(golden);
    expect(serializeGraph(parseGraph(text))).toBe(fixture("graph.golden.ndjson"));
  });

  it("ignores a leading byte-order mark", () => {
    const text = fixture("graph.golden.ndjson");
    expect(parseGraph(`\uFEFF${text}`)).toEqual(golden);
    expect(parseGraph(`\uFEFF${fixture("graph.handwritten.ndjson")}`)).toEqual(golden);
  });

  it("treats an empty file as an empty graph", () => {
    expect(parseGraph("")).toEqual([]);
    expect(serializeGraph([])).toBe("");
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

describe("serialization", () => {
  it("omits null and empty values but always writes kind and status", () => {
    const line = serializeGraph([
      {
        id: ROOT,
        parent: null,
        title: "t",
        kind: "work",
        status: "todo",
        depends_on: [],
        delivery: { repo: "app", commits: [] },
      },
    ]);
    expect(line).toBe(
      `{"id":"${ROOT}","title":"t","kind":"work","status":"todo","delivery":{"repo":"app"}}\n`,
    );
  });

  describe("refuses to write a node it couldn't read back", () => {
    const base: Node = {
      id: ROOT,
      parent: null,
      title: "t",
      kind: "work",
      status: "todo",
      depends_on: [],
    };
    it.each<[string, Partial<Node>, RegExp]>([
      ["a multi-line title", { title: "a\nb" }, /title: must be a single line/],
      ["a blank title", { title: "  " }, /title: must not be blank/],
      [
        "a non-v7 id",
        { id: "0192f2c8-1a0d-4e33-8c41-5b2a9d0e7f10" },
        /id: must be a lowercase UUIDv7/,
      ],
      ["a bad parent", { parent: "nope" }, /parent: must be a lowercase UUIDv7/],
      ["a bad dependency", { depends_on: ["nope"] }, /depends_on\[0\]: /],
      [
        "a non-hex commit",
        { delivery: { repo: "app", commits: ["zz"] } },
        /delivery\.commits\[0\]: /,
      ],
      ["an empty delivery repo", { delivery: { repo: "" } }, /delivery\.repo: /],
      ["a non-URL pr", { delivery: { repo: "app", pr: "12" } }, /delivery\.pr: /],
      ["an unknown status", { status: "blocked" as Node["status"] }, /status: /],
    ])("%s", (_name, patch, pattern) => {
      const bad = { ...base, ...patch };
      expect(() => serializeGraph([golden[0] as Node, bad])).toThrow(InvalidNodeError);
      expect(() => serializeGraph([bad])).toThrow(pattern);
    });
  });

  it("refuses duplicate ids", () => {
    const n = golden[0] as Node;
    expect(() => serializeGraph([n, { ...n }])).toThrow(/duplicate id/);
  });
});

describe("round-trip properties", () => {
  const id = fc.uuid({ version: 7 });
  const optional = <T>(arb: fc.Arbitrary<T>) => fc.option(arb, { nil: undefined });
  const text = fc
    .string({ unit: "binary", minLength: 1, maxLength: 20 })
    .filter((s) => s.trim() !== "" && !/[\r\n]/.test(s));
  const word = fc.string({ unit: "binary", minLength: 1, maxLength: 10 });
  const sha = fc.string({
    unit: fc.constantFrom(..."0123456789abcdef"),
    minLength: 7,
    maxLength: 40,
  });

  // Canonical nodes: what parseGraph returns. References needn't resolve: that's P03's job.
  const node: fc.Arbitrary<Node> = fc
    .record({
      id,
      parent: fc.option(id, { nil: null }),
      title: text,
      kind: fc.constantFrom(...KINDS),
      status: fc.constantFrom(...STATUSES),
      depends_on: fc.uniqueArray(id, { maxLength: 4 }).map((d) => d.sort()),
      delivery: optional(
        fc.record(
          {
            repo: word,
            branch: word,
            commits: fc.array(sha, { minLength: 1, maxLength: 3 }),
            pr: fc.webUrl(),
          },
          { requiredKeys: ["repo"] },
        ),
      ),
      external: optional(
        fc.record(
          { provider: word, id: word, synced_at: word, synced_hash: word },
          { requiredKeys: ["provider", "id"] },
        ),
      ),
      variant_of: optional(id),
    })
    .map((r) => {
      const n: Node = {
        id: r.id,
        parent: r.parent,
        title: r.title,
        kind: r.kind,
        status: r.status,
        depends_on: r.depends_on,
      };
      if (r.delivery) n.delivery = r.delivery;
      if (r.external) n.external = r.external;
      if (r.variant_of) n.variant_of = r.variant_of;
      return n;
    });
  const graph = fc.uniqueArray(node, { selector: (n) => n.id, maxLength: 15 });
  const sortedById = (nodes: Node[]) => [...nodes].sort((a, b) => (a.id < b.id ? -1 : 1));

  it("parse(serialize(g)) ≡ g", () => {
    fc.assert(
      fc.property(graph, (g) => {
        expect(parseGraph(serializeGraph(g))).toEqual(sortedById(g));
      }),
    );
  });

  it("whatever serializeGraph writes, parseGraph reads back (or it refuses to write)", () => {
    // Deliberately loose: values that are often invalid, to exercise the write-side check.
    const looseId = fc.oneof(id, fc.uuid(), fc.string({ maxLength: 8 }));
    const looseText = fc.string({ unit: "binary", maxLength: 8 });
    const loose: fc.Arbitrary<Node> = fc
      .record({
        id: looseId,
        parent: fc.option(looseId, { nil: null }),
        title: fc.oneof(text, looseText, fc.constantFrom("", " ", "a\nb", "a\rb")),
        kind: fc.constantFrom(...KINDS),
        status: fc.constantFrom(...STATUSES),
        depends_on: fc.array(looseId, { maxLength: 3 }),
        repo: fc.option(looseText, { nil: undefined }),
        commits: fc.array(fc.oneof(sha, looseText), { maxLength: 2 }),
        pr: fc.option(fc.oneof(fc.webUrl(), looseText), { nil: undefined }),
      })
      .map(({ repo, commits, pr, ...rest }) => {
        const n: Node = rest;
        if (repo !== undefined) {
          n.delivery = { repo, commits };
          if (pr !== undefined) n.delivery.pr = pr;
        }
        return n;
      });
    fc.assert(
      fc.property(fc.uniqueArray(loose, { selector: (n) => n.id, maxLength: 5 }), (g) => {
        let text: string;
        try {
          text = serializeGraph(g);
        } catch (err) {
          expect(err).toBeInstanceOf(InvalidNodeError);
          return;
        }
        expect(serializeGraph(parseGraph(text))).toBe(text);
      }),
      { numRuns: 500 },
    );
  });

  it("serialization is byte-stable and independent of input order", () => {
    fc.assert(
      fc.property(
        graph.chain((g) =>
          fc.tuple(fc.constant(g), fc.shuffledSubarray(g, { minLength: g.length })),
        ),
        ([g, shuffled]) => {
          const text = serializeGraph(g);
          expect(serializeGraph(shuffled)).toBe(text);
          expect(serializeGraph(parseGraph(text))).toBe(text);
        },
      ),
    );
  });
});
