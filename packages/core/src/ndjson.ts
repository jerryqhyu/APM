// graph.ndjson: one node per line (plans.md §3.2). Parsing accepts hand-written files and
// returns the graph in canonical form.
//
// Parsing checks each line on its own (shape, types, ID format, duplicates). Checks that need
// the whole graph, like parents existing or cycles, are the invariants in P03.

import { z } from "zod";
import { UUID_V7_RE } from "./ids.ts";
import { KINDS, type Node, type NodeId, STATUSES } from "./model.ts";

export interface ParseIssue {
  /** 1-based line number. */
  line: number;
  message: string;
}

export class GraphParseError extends Error {
  override name = "GraphParseError";
  readonly issues: readonly ParseIssue[];
  readonly source: string;

  constructor(issues: readonly ParseIssue[], source = "graph.ndjson") {
    super(issues.map((i) => `${source}:${i.line}: ${i.message}`).join("\n"));
    this.issues = issues;
    this.source = source;
  }
}

// Optional fields accept null in hand-written files and treat it as absent.
const nodeId = z.string().regex(UUID_V7_RE, "must be a lowercase UUIDv7");
const nonBlank = z
  .string()
  .refine((s) => s.trim().length > 0, "must not be blank")
  .refine((s) => !/[\r\n]/.test(s), "must be a single line");

const DeliverySchema = z.strictObject({
  repo: z.string().min(1),
  branch: z.string().min(1).nullish(),
  commits: z.array(z.string().regex(/^[0-9a-f]{7,64}$/, "must be a hex commit SHA")).nullish(),
  pr: z.url().nullish(),
});

const ExternalSchema = z.strictObject({
  provider: z.string().min(1),
  id: z.string().min(1),
  synced_at: z.string().min(1).nullish(),
  synced_hash: z.string().min(1).nullish(),
});

const NodeLineSchema = z.strictObject({
  id: nodeId,
  parent: nodeId.nullish(),
  title: nonBlank,
  kind: z.enum(KINDS).nullish(),
  status: z.enum(STATUSES).nullish(),
  depends_on: z.array(nodeId).nullish(),
  delivery: DeliverySchema.nullish(),
  external: ExternalSchema.nullish(),
  variant_of: nodeId.nullish(),
});

type NodeLine = z.infer<typeof NodeLineSchema>;

/** Parses graph.ndjson text. Returns nodes in canonical form, sorted by id. */
export function parseGraph(text: string, source = "graph.ndjson"): Node[] {
  const issues: ParseIssue[] = [];
  const firstSeen = new Map<NodeId, number>();
  const nodes: Node[] = [];

  // Some editors start files with a byte-order mark; it isn't part of line 1's JSON.
  const body = text.startsWith("\uFEFF") ? text.slice(1) : text;
  for (const [i, raw] of body.split("\n").entries()) {
    const line = i + 1;
    const content = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (content.trim() === "") continue;

    let json: unknown;
    try {
      json = JSON.parse(content);
    } catch (err) {
      issues.push({ line, message: `invalid JSON: ${(err as Error).message}` });
      continue;
    }
    const result = NodeLineSchema.safeParse(json);
    if (!result.success) {
      for (const message of formatIssues(result.error.issues)) issues.push({ line, message });
      continue;
    }
    const node = fromLine(result.data);
    const prev = firstSeen.get(node.id);
    if (prev !== undefined) {
      issues.push({ line, message: `duplicate id ${node.id} (first on line ${prev})` });
      continue;
    }
    firstSeen.set(node.id, line);
    nodes.push(node);
  }

  if (issues.length > 0) throw new GraphParseError(issues, source);
  return nodes.sort(byId);
}

/** Sorted, de-duplicated copy. UUIDv7s sort by creation time. */
export function canonicalIds(ids: Iterable<NodeId>): NodeId[] {
  return [...new Set(ids)].sort();
}

function fromLine(l: NodeLine): Node {
  const node: Node = {
    id: l.id,
    parent: l.parent ?? null,
    title: l.title,
    kind: l.kind ?? "work",
    status: l.status ?? "todo",
    depends_on: canonicalIds(l.depends_on ?? []),
  };
  if (l.delivery) {
    node.delivery = { repo: l.delivery.repo };
    if (l.delivery.branch) node.delivery.branch = l.delivery.branch;
    if (l.delivery.commits && l.delivery.commits.length > 0) {
      node.delivery.commits = l.delivery.commits;
    }
    if (l.delivery.pr) node.delivery.pr = l.delivery.pr;
  }
  if (l.external) {
    node.external = { provider: l.external.provider, id: l.external.id };
    if (l.external.synced_at) node.external.synced_at = l.external.synced_at;
    if (l.external.synced_hash) node.external.synced_hash = l.external.synced_hash;
  }
  if (l.variant_of) node.variant_of = l.variant_of;
  return node;
}

function byId(a: Node, b: Node): number {
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

function formatIssues(issues: readonly z.core.$ZodIssue[]): string[] {
  return issues.map((i) => (i.path.length > 0 ? `${formatPath(i.path)}: ${i.message}` : i.message));
}

function formatPath(path: readonly PropertyKey[]): string {
  return path
    .map((p, i) => (typeof p === "number" ? `[${p}]` : i === 0 ? String(p) : `.${String(p)}`))
    .join("");
}
