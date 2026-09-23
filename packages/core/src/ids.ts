// UUIDv7 IDs and short handles (plans.md §3.7).
//
// The handle is the LAST 8 hex chars. The leading chars are the timestamp, so nodes created
// close together share them; the trailing chars are random.

import { v7 } from "uuid";
import type { NodeId } from "./model.ts";

export const UUID_V7_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export const HANDLE_LENGTH = 8;
/** Shortest suffix `resolveHandle` accepts. */
export const MIN_HANDLE_INPUT = 4;

/** A new ID. IDs from one process sort in creation order, even within the same millisecond. */
export function newId(): NodeId {
  return v7();
}

export function isNodeId(value: string): boolean {
  return UUID_V7_RE.test(value);
}

export function handle(id: NodeId): string {
  return id.slice(-HANDLE_LENGTH);
}

/**
 * Display handles for a set of IDs: the last 8 hex chars, lengthened only for IDs whose last 8
 * collide with another's, until they're unique. (32 random bits: collisions are rare but real
 * in large projects.)
 */
export function handles(ids: Iterable<NodeId>): Map<NodeId, string> {
  // IDs sharing a longer suffix also share the last 8, so only same-handle groups need work.
  const groups = new Map<string, NodeId[]>();
  for (const id of ids) {
    const group = groups.get(handle(id));
    if (group) group.push(id);
    else groups.set(handle(id), [id]);
  }
  const out = new Map<NodeId, string>();
  for (const [short, group] of groups) {
    if (group.length === 1) {
      out.set(group[0] as NodeId, short);
      continue;
    }
    const hexes = group.map(hex);
    for (const [i, h] of hexes.entries()) {
      let len = HANDLE_LENGTH;
      while (len < h.length && hexes.some((other, j) => j !== i && other.endsWith(h.slice(-len)))) {
        len++;
      }
      out.set(group[i] as NodeId, h.slice(-len));
    }
  }
  return out;
}

export type HandleErrorCode = "invalid" | "unknown" | "ambiguous";

export class HandleError extends Error {
  override name = "HandleError";
  readonly code: HandleErrorCode;
  readonly input: string;
  /** For `ambiguous`: every matching ID. */
  readonly candidates: readonly NodeId[];

  constructor(
    code: HandleErrorCode,
    input: string,
    message: string,
    candidates: readonly NodeId[] = [],
  ) {
    super(message);
    this.code = code;
    this.input = input;
    this.candidates = candidates;
  }
}

/**
 * Resolves user input to one of `ids`. Accepts any unambiguous hex suffix of at least 4 chars,
 * or a full ID with or without hyphens, in any case.
 */
export function resolveHandle(input: string, ids: Iterable<NodeId>): NodeId {
  const needle = input.trim().toLowerCase().replaceAll("-", "");
  if (!/^[0-9a-f]+$/.test(needle) || needle.length < MIN_HANDLE_INPUT || needle.length > 32) {
    throw new HandleError(
      "invalid",
      input,
      `invalid handle "${input}": expected at least ${MIN_HANDLE_INPUT} hex characters from the end of a node ID`,
    );
  }
  const matches = [...ids].filter((id) => hex(id).endsWith(needle));
  if (matches.length === 0) {
    throw new HandleError("unknown", input, `no node matches handle "${input}"`);
  }
  if (matches.length > 1) {
    const shown = handles(matches);
    const list = matches.map((id) => shown.get(id)).join(", ");
    throw new HandleError(
      "ambiguous",
      input,
      `handle "${input}" matches ${matches.length} nodes: ${list}`,
      matches,
    );
  }
  return matches[0] as NodeId;
}

function hex(id: NodeId): string {
  return id.replaceAll("-", "");
}
