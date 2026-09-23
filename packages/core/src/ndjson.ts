// graph.ndjson: one node per line, sorted by id, fixed key order, null and empty values left
// out (plans.md §3.2). The output is byte-stable: the same graph always serializes identically.
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

/** Thrown when asked to write a node that `parseGraph` would reject. */
export class InvalidNodeError extends Error {
  override name = "InvalidNodeError";
  readonly id: unknown;
  readonly issues: readonly string[];

  constructor(id: unknown, issues: readonly string[]) {
    super(`cannot write node ${String(id)}: ${issues.join("; ")}`);
    this.id = id;
    this.issues = issues;
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

/** Serializes nodes to graph.ndjson text. Input order doesn't matter; output is canonical. */
export function serializeGraph(nodes: Iterable<Node>): string {
  const sorted = [...nodes].sort(byId);
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i]?.id === sorted[i - 1]?.id) {
      throw new Error(`serializeGraph: duplicate id ${sorted[i]?.id}`);
    }
  }
  return sorted.map((n) => `${serializeNode(n)}\n`).join("");
}

/**
 * One line of graph.ndjson (without the newline). Keys always in the order below.
 * Throws `InvalidNodeError` rather than produce a line `parseGraph` would reject, so a bad value
 * can never make the file unreadable.
 */
export function serializeNode(n: Node): string {
  const out: Record<string, unknown> = { id: n.id };
  if (n.parent !== null) out.parent = n.parent;
  out.title = n.title;
  out.kind = n.kind;
  out.status = n.status;
  const deps = canonicalIds(n.depends_on);
  if (deps.length > 0) out.depends_on = deps;
  if (n.delivery) {
    const d = n.delivery;
    const delivery: Record<string, unknown> = { repo: d.repo };
    if (d.branch) delivery.branch = d.branch;
    if (d.commits && d.commits.length > 0) delivery.commits = d.commits;
    if (d.pr) delivery.pr = d.pr;
    out.delivery = delivery;
  }
  if (n.external) {
    const e = n.external;
    const external: Record<string, unknown> = { provider: e.provider, id: e.id };
    if (e.synced_at) external.synced_at = e.synced_at;
    if (e.synced_hash) external.synced_hash = e.synced_hash;
    out.external = external;
  }
  if (n.variant_of) out.variant_of = n.variant_of;
  const check = NodeLineSchema.safeParse(out);
  if (!check.success) throw new InvalidNodeError(n.id, formatIssues(check.error.issues));
  return JSON.stringify(out);
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
