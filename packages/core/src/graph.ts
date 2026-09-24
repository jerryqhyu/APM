// The in-memory graph (plans.md §2.1): nodes by id, a children index, and containment walks.
//
// A Graph is read-only. It doesn't check invariants (that's validate(), P03.2), so its walks
// must stay safe on broken input: a missing parent ends a walk, and a containment cycle can't
// make one loop forever.

import type { Node, NodeId } from "./model.ts";

export class Graph {
  readonly nodes: ReadonlyMap<NodeId, Node>;
  // Keyed by parent id (null = top level). A missing parent's orphans are indexed under its id.
  readonly #children = new Map<NodeId | null, NodeId[]>();

  constructor(nodes: Iterable<Node>) {
    const byId = new Map<NodeId, Node>();
    for (const node of nodes) {
      if (byId.has(node.id)) throw new Error(`Graph: duplicate id ${node.id}`);
      byId.set(node.id, node);
    }
    this.nodes = byId;
    for (const id of [...byId.keys()].sort()) {
      const parent = (byId.get(id) as Node).parent;
      const siblings = this.#children.get(parent);
      if (siblings) siblings.push(id);
      else this.#children.set(parent, [id]);
    }
  }

  get size(): number {
    return this.nodes.size;
  }

  has(id: NodeId): boolean {
    return this.nodes.has(id);
  }

  /** The node with this id. Throws if there isn't one. */
  get(id: NodeId): Node {
    const node = this.nodes.get(id);
    if (!node) throw new Error(`Graph: unknown node ${id}`);
    return node;
  }

  /** Children of `parent` (null = top-level nodes), in id order, which is creation order. */
  children(parent: NodeId | null): readonly NodeId[] {
    return this.#children.get(parent) ?? [];
  }

  isLeaf(id: NodeId): boolean {
    return this.children(id).length === 0;
  }

  /** Ancestors, nearest first. Stops at a parent that doesn't exist, or on a cycle. */
  ancestors(id: NodeId): NodeId[] {
    const out: NodeId[] = [];
    const seen = new Set<NodeId>([id]);
    let parent = this.get(id).parent;
    while (parent !== null && !seen.has(parent)) {
      const node = this.nodes.get(parent);
      if (!node) break;
      out.push(parent);
      seen.add(parent);
      parent = node.parent;
    }
    return out;
  }

  /** Number of ancestors: 0 for a top-level node. */
  depth(id: NodeId): number {
    return this.ancestors(id).length;
  }

  /** All descendants, depth-first in id order, not including `id` itself. */
  descendants(id: NodeId): NodeId[] {
    this.get(id);
    const out: NodeId[] = [];
    const seen = new Set<NodeId>([id]);
    const visit = (parent: NodeId) => {
      for (const child of this.children(parent)) {
        if (seen.has(child)) continue;
        seen.add(child);
        out.push(child);
        visit(child);
      }
    };
    visit(id);
    return out;
  }

  /** True if `ancestor` is a strict ancestor of `id`. */
  isAncestor(ancestor: NodeId, id: NodeId): boolean {
    return this.ancestors(id).includes(ancestor);
  }
}
