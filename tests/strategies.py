"""Hypothesis strategies shared by tests across phases."""

import uuid

from hypothesis import strategies as st

from apm.core.model import Delivery, Kind, Node, Status


def node_ids() -> st.SearchStrategy[str]:
    """Lowercase UUIDv7 strings with random bits (not time-ordered)."""
    return st.integers(0, 2**128 - 1).map(lambda n: str(uuid.UUID(int=n, version=7)))


@st.composite
def graphs(draw: st.DrawFn, *, max_nodes: int = 12) -> list[Node]:
    """Valid graphs: containment is a tree (I1), `depends_on` is acyclic (I2), never points at an
    ancestor or descendant (I3) and always at an existing node (I4), and only leaves carry
    `delivery` (I7). Kind and status are unconstrained, so I6 warnings can occur.

    Parents and dependencies follow two independent random orders rather than id order, so a child
    can be older than its parent and a dependency newer than its dependent, as they can be after
    moves and later links.
    """
    n = draw(st.integers(0, max_nodes))
    ids = draw(st.lists(node_ids(), min_size=n, max_size=n, unique=True))
    tree_order = draw(st.permutations(range(n)))
    rank = {idx: k for k, idx in enumerate(draw(st.permutations(range(n))))}
    picks = st.tuples(
        st.integers(0, 2**16),
        st.lists(st.integers(0, 2**16), max_size=3),
        st.sampled_from(Kind),
        st.sampled_from(Status),
        st.booleans(),
    )
    pick = draw(st.lists(picks, min_size=n, max_size=n))

    # Each node's parent is one placed before it in tree_order, or none.
    parent: dict[int, int | None] = {}
    for k, idx in enumerate(tree_order):
        r = pick[idx][0] % (k + 1)
        parent[idx] = None if r == k else tree_order[r]

    def ancestors(idx: int) -> set[int]:
        out: set[int] = set()
        p = parent[idx]
        while p is not None:
            out.add(p)
            p = parent[p]
        return out

    anc = [ancestors(idx) for idx in range(n)]
    has_children = {p for p in parent.values() if p is not None}

    nodes = []
    for idx, node_id in enumerate(ids):
        _, dep_picks, kind, status, deliver = pick[idx]
        # Dependencies point only at nodes ranked lower, so they can't form a cycle.
        candidates = [
            other
            for other in range(n)
            if rank[other] < rank[idx] and other not in anc[idx] and idx not in anc[other]
        ]
        deps = {ids[candidates[d % len(candidates)]] for d in dep_picks} if candidates else set()
        p = parent[idx]
        nodes.append(
            Node(
                id=node_id,
                parent=None if p is None else ids[p],
                title=f"node {idx}",
                kind=kind,
                status=status,
                depends_on=tuple(sorted(deps)),
                delivery=Delivery(repo="app") if deliver and idx not in has_children else None,
            )
        )
    return nodes
