// fast-check generators shared by tests across phases. Not part of the published build.

import fc from "fast-check";
import { KINDS, type Node, STATUSES } from "../model.ts";

export interface ArbGraphOptions {
  maxNodes?: number;
}

/**
 * Valid graphs: containment is a tree (I1), `depends_on` is acyclic (I2), never points at an
 * ancestor or descendant (I3) and always at an existing node (I4), and only leaves carry
 * `delivery` (I7). Kind and status are unconstrained, so I6 warnings can occur.
 *
 * Parents and dependencies follow two independent random orders rather than id order, so a
 * child can be older than its parent and a dependency can be newer than its dependent, as they
 * can be after moves and later links.
 */
export function arbGraph({ maxNodes = 12 }: ArbGraphOptions = {}): fc.Arbitrary<Node[]> {
  return fc
    .integer({ min: 0, max: maxNodes })
    .chain((n) => {
      const indices = [...Array(n).keys()];
      return fc.record({
        ids: fc.uniqueArray(fc.uuid({ version: 7 }), { minLength: n, maxLength: n }),
        treeOrder: fc.shuffledSubarray(indices, { minLength: n }),
        depOrder: fc.shuffledSubarray(indices, { minLength: n }),
        picks: fc.array(
          fc.record({
            parent: fc.nat(),
            deps: fc.array(fc.nat(), { maxLength: 3 }),
            kind: fc.constantFrom(...KINDS),
            status: fc.constantFrom(...STATUSES),
            deliver: fc.boolean(),
          }),
          { minLength: n, maxLength: n },
        ),
      });
    })
    .map(({ ids, treeOrder, depOrder, picks }) => {
      const at = <T>(xs: readonly T[], i: number) => xs[i] as T;

      // Each node's parent is one placed before it in treeOrder, or none.
      const parent = new Map<number, number | null>();
      for (const [k, idx] of treeOrder.entries()) {
        const r = at(picks, idx).parent % (k + 1);
        parent.set(idx, r === k ? null : at(treeOrder, r));
      }
      const ancestors = (idx: number) => {
        const out = new Set<number>();
        for (let p = parent.get(idx) ?? null; p !== null; p = parent.get(p) ?? null) out.add(p);
        return out;
      };
      const anc = ids.map((_, idx) => ancestors(idx));
      const hasChildren = new Set([...parent.values()].filter((p) => p !== null));

      // Dependencies point only at nodes earlier in depOrder, so they can't form a cycle.
      const rank = new Map(depOrder.map((idx, k) => [idx, k]));
      return ids.map((id, idx): Node => {
        const pick = at(picks, idx);
        const candidates = ids
          .map((_, other) => other)
          .filter(
            (other) =>
              (rank.get(other) as number) < (rank.get(idx) as number) &&
              !at(anc, idx).has(other) &&
              !at(anc, other).has(idx),
          );
        const deps =
          candidates.length === 0
            ? []
            : pick.deps.map((d) => at(ids, at(candidates, d % candidates.length)));
        const p = parent.get(idx) ?? null;
        const node: Node = {
          id,
          parent: p === null ? null : at(ids, p),
          title: `node ${idx}`,
          kind: pick.kind,
          status: pick.status,
          depends_on: [...new Set(deps)].sort(),
        };
        if (pick.deliver && !hasChildren.has(idx)) node.delivery = { repo: "app" };
        return node;
      });
    });
}
