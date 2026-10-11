import pytest
from hypothesis import Phase, find, given, settings

from apm.core.graph import Graph
from apm.core.model import Node, NodeId
from tests.strategies import graphs

# Ids in creation order: A < B < C < D < E < F.
A, B, C, D, E, F = (f"01930000-0000-7000-8000-00000000000{c}" for c in "abcdef")


def node(node_id: NodeId, parent: NodeId | None) -> Node:
    return Node(id=node_id, parent=parent, title=node_id[-1])


#   A        F
#  / \
# C   B      (C is listed before B in the input; children come back in id order)
#     |
#     D
#     |
#     E
FIXTURE = Graph([node(C, A), node(A, None), node(E, D), node(B, A), node(D, B), node(F, None)])


def test_indexes_children_in_id_order_with_none_for_the_top_level() -> None:
    assert FIXTURE.children(None) == (A, F)
    assert FIXTURE.children(A) == (B, C)
    assert FIXTURE.children(E) == ()
    assert len(FIXTURE) == 6


def test_walks_ancestors_nearest_first_and_computes_depth() -> None:
    assert FIXTURE.ancestors(E) == [D, B, A]
    assert FIXTURE.ancestors(A) == []
    assert FIXTURE.depth(E) == 3
    assert FIXTURE.depth(F) == 0
    assert FIXTURE.is_ancestor(A, E)
    assert not FIXTURE.is_ancestor(E, A)
    assert not FIXTURE.is_ancestor(E, E)


def test_lists_descendants_depth_first_and_knows_leaves() -> None:
    assert FIXTURE.descendants(A) == [B, D, E, C]
    assert FIXTURE.descendants(E) == []
    assert FIXTURE.is_leaf(E)
    assert not FIXTURE.is_leaf(A)


def test_rejects_duplicate_ids_and_unknown_lookups() -> None:
    with pytest.raises(ValueError, match="duplicate id"):
        Graph([node(A, None), node(A, None)])
    unknown = A[:-1] + "1"
    with pytest.raises(KeyError, match="unknown node"):
        FIXTURE[unknown]
    with pytest.raises(KeyError, match="unknown node"):
        FIXTURE.descendants(unknown)
    assert A in FIXTURE
    assert unknown not in FIXTURE


def test_stays_safe_on_a_missing_parent_or_a_containment_cycle() -> None:
    orphan = Graph([node(B, A)])
    assert orphan.ancestors(B) == []
    assert orphan.children(A) == (B,)

    cycle = Graph([node(A, C), node(B, A), node(C, B)])
    assert cycle.ancestors(A) == [C, B]
    assert cycle.descendants(A) == [B, C]
    assert cycle.children(None) == ()


def test_walks_a_deep_chain_without_hitting_the_recursion_limit() -> None:
    ids = [f"01930000-0000-7000-8000-{i:012x}" for i in range(5000)]
    chain = Graph(node(i, p) for p, i in zip([None, *ids], ids, strict=False))
    assert chain.depth(ids[-1]) == 4999
    assert len(chain.descendants(ids[0])) == 4999


def naive(nodes: list[Node]):
    """Slow but obviously correct versions of the walks, to check Graph against."""
    by_id = {n.id: n for n in nodes}

    def ancestors(node_id: NodeId) -> list[NodeId]:
        p = by_id[node_id].parent
        return [] if p is None else [p, *ancestors(p)]

    def children(node_id: NodeId | None) -> tuple[NodeId, ...]:
        return tuple(sorted(n.id for n in nodes if n.parent == node_id))

    def descendants(node_id: NodeId) -> list[NodeId]:
        return sorted(n.id for n in nodes if node_id in ancestors(n.id))

    return ancestors, children, descendants


@given(graphs())
def test_walks_agree_with_a_naive_recomputation(nodes: list[Node]) -> None:
    graph = Graph(nodes)
    ancestors, children, descendants = naive(nodes)
    assert graph.children(None) == children(None)
    for n in nodes:
        assert graph.children(n.id) == children(n.id)
        assert graph.ancestors(n.id) == ancestors(n.id)
        assert graph.depth(n.id) == len(ancestors(n.id))
        assert sorted(graph.descendants(n.id)) == descendants(n.id)


@given(graphs())
def test_generated_graphs_are_valid(nodes: list[Node]) -> None:
    """I1 to I4 and I7, checked independently of the strategy that built them."""
    graph = Graph(nodes)
    for n in nodes:
        assert n.parent is None or n.parent in graph  # I1: the parent exists
        for dep in n.depends_on:
            assert dep in graph  # I4
            assert not graph.is_ancestor(dep, n.id)  # I3
            assert not graph.is_ancestor(n.id, dep)
        if n.delivery is not None:
            assert graph.is_leaf(n.id)  # I7
    reached = {d for top in graph.children(None) for d in [top, *graph.descendants(top)]}
    assert len(reached) == len(nodes)  # I1: no containment cycles
    # I2: repeatedly removing nodes with no remaining dependencies empties the graph.
    remaining = {n.id: set(n.depends_on) for n in nodes}
    while ready := [i for i, deps in remaining.items() if not deps & remaining.keys()]:
        for i in ready:
            del remaining[i]
    assert not remaining


SHAPES = {
    "deep": lambda g: any(g.depth(i) >= 3 for i in g.nodes),
    "cross-level dependency": lambda g: any(
        g[d].parent != n.parent for n in g.nodes.values() for d in n.depends_on
    ),
    "child older than its parent": lambda g: any(
        n.parent is not None and n.parent > n.id for n in g.nodes.values()
    ),
}


@pytest.mark.parametrize("shape", SHAPES)
def test_generated_graphs_cover_the_interesting_shapes(shape: str) -> None:
    # Generation only: the test needs an example to exist, not the smallest one.
    only_generate = settings(max_examples=2000, database=None, phases=[Phase.generate])
    find(graphs().map(Graph), SHAPES[shape], settings=only_generate)
