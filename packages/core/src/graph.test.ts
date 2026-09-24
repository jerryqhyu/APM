import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { Graph } from "./graph.ts";
import type { Node, NodeId } from "./model.ts";
import { arbGraph } from "./testing/arbitraries.ts";

// Ids in creation order: A < B < C < D < E < F.
const A = "01930000-0000-7000-8000-00000000000a";
const B = "01930000-0000-7000-8000-00000000000b";
const C = "01930000-0000-7000-8000-00000000000c";
const D = "01930000-0000-7000-8000-00000000000d";
const E = "01930000-0000-7000-8000-00000000000e";
const F = "01930000-0000-7000-8000-00000000000f";

const node = (id: NodeId, parent: NodeId | null): Node => ({
  id,
  parent,
  title: id.slice(-1),
  kind: "work",
  status: "todo",
  depends_on: [],
});

//   A        F
//  / \
// C   B      (C is listed before B in the input; children come back in id order)
//     |
//     D
//     |
//     E
const fixture = [node(C, A), node(A, null), node(E, D), node(B, A), node(D, B), node(F, null)];

describe("Graph", () => {
  const g = new Graph(fixture);

  it("indexes children in id order, with null for the top level", () => {
    expect(g.children(null)).toEqual([A, F]);
    expect(g.children(A)).toEqual([B, C]);
    expect(g.children(E)).toEqual([]);
    expect(g.size).toBe(6);
  });

  it("walks ancestors nearest first, and computes depth", () => {
    expect(g.ancestors(E)).toEqual([D, B, A]);
    expect(g.ancestors(A)).toEqual([]);
    expect(g.depth(E)).toBe(3);
    expect(g.depth(F)).toBe(0);
    expect(g.isAncestor(A, E)).toBe(true);
    expect(g.isAncestor(E, A)).toBe(false);
    expect(g.isAncestor(E, E)).toBe(false);
  });

  it("lists descendants depth-first, and knows leaves", () => {
    expect(g.descendants(A)).toEqual([B, D, E, C]);
    expect(g.descendants(E)).toEqual([]);
    expect(g.isLeaf(E)).toBe(true);
    expect(g.isLeaf(A)).toBe(false);
  });

  it("rejects duplicate ids and unknown lookups", () => {
    expect(() => new Graph([node(A, null), node(A, null)])).toThrow(/duplicate id/);
    expect(() => g.get(`${A.slice(0, -1)}1`)).toThrow(/unknown node/);
    expect(g.has(A)).toBe(true);
  });

  it("stays safe on broken input: a missing parent or a containment cycle", () => {
    const orphan = new Graph([node(B, A)]);
    expect(orphan.ancestors(B)).toEqual([]);
    expect(orphan.children(A)).toEqual([B]);

    const cycle = new Graph([node(A, C), node(B, A), node(C, B)]);
    expect(cycle.ancestors(A)).toEqual([C, B]);
    expect(cycle.descendants(A)).toEqual([B, C]);
    expect(cycle.children(null)).toEqual([]);
  });
});

// Slow but obviously correct versions of the walks, to check Graph against.
function naive(nodes: readonly Node[]) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const ancestors = (id: NodeId): NodeId[] => {
    const p = byId.get(id)?.parent ?? null;
    return p === null ? [] : [p, ...ancestors(p)];
  };
  return {
    ancestors,
    children: (id: NodeId | null) =>
      nodes
        .filter((n) => n.parent === id)
        .map((n) => n.id)
        .sort(),
    descendants: (id: NodeId) =>
      nodes
        .filter((n) => ancestors(n.id).includes(id))
        .map((n) => n.id)
        .sort(),
  };
}

describe("arbGraph", () => {
  it("Graph's walks agree with a naive recomputation", () => {
    fc.assert(
      fc.property(arbGraph(), (nodes) => {
        const g = new Graph(nodes);
        const slow = naive(nodes);
        expect(g.children(null)).toEqual(slow.children(null));
        for (const { id } of nodes) {
          expect(g.children(id)).toEqual(slow.children(id));
          expect(g.ancestors(id)).toEqual(slow.ancestors(id));
          expect(g.depth(id)).toBe(slow.ancestors(id).length);
          expect([...g.descendants(id)].sort()).toEqual(slow.descendants(id));
        }
      }),
    );
  });

  it("generates only valid graphs (I1–I4, I7)", () => {
    fc.assert(
      fc.property(arbGraph(), (nodes) => {
        const g = new Graph(nodes);
        const reached = new Set<NodeId>();
        for (const n of nodes) {
          // I1: the parent exists and the walk reaches the top level (the naive walk above
          // would overflow the stack on a cycle).
          if (n.parent !== null) expect(g.has(n.parent)).toBe(true);
          for (const dep of n.depends_on) {
            expect(g.has(dep)).toBe(true); // I4
            expect(g.isAncestor(dep, n.id) || g.isAncestor(n.id, dep)).toBe(false); // I3
          }
          if (n.delivery) expect(g.isLeaf(n.id)).toBe(true); // I7
        }
        for (const top of g.children(null)) {
          reached.add(top);
          for (const d of g.descendants(top)) reached.add(d);
        }
        expect(reached.size).toBe(nodes.length); // I1: no containment cycles
        // I2: repeatedly removing nodes with no remaining dependencies empties the graph.
        const remaining = new Map(nodes.map((n) => [n.id, new Set(n.depends_on)]));
        let progressed = true;
        while (progressed) {
          progressed = false;
          for (const [id, deps] of remaining) {
            if ([...deps].every((d) => !remaining.has(d))) {
              remaining.delete(id);
              progressed = true;
            }
          }
        }
        expect(remaining.size).toBe(0);
      }),
    );
  });

  it("covers the interesting shapes", () => {
    const samples = fc.sample(arbGraph(), { numRuns: 200, seed: 1 });
    const deep = samples.some((nodes) => nodes.some((n) => new Graph(nodes).depth(n.id) >= 3));
    const crossLevel = samples.some((nodes) => {
      const g = new Graph(nodes);
      return nodes.some((n) => n.depends_on.some((d) => g.get(d).parent !== n.parent));
    });
    const olderChild = samples.some((nodes) => nodes.some((n) => n.parent && n.parent > n.id));
    expect({ deep, crossLevel, olderChild }).toEqual({
      deep: true,
      crossLevel: true,
      olderChild: true,
    });
  });
});
