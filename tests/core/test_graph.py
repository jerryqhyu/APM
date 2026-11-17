import pytest
from hypothesis import Phase, find, given, settings

from apm.core.graph import Graph, Progress
from apm.core.model import Node, NodeId, Status
from tests.core.build import A, B, C, D, E, F, make_id, node
from tests.strategies import graphs

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
    unknown = make_id(1)
    with pytest.raises(KeyError, match="unknown node"):
        FIXTURE[unknown]
    with pytest.raises(KeyError, match="unknown node"):
        FIXTURE.descendants(unknown)
    assert A in FIXTURE
    assert unknown not in FIXTURE


def test_indexes_dependencies_both_ways_and_lists_leaves() -> None:
    graph = Graph([node(A), node(B, A, deps=[D, C]), node(C, A), node(D, deps=[C])])
    assert graph.dependencies(B) == (C, D)
    assert graph.dependents(C) == (B, D)
    assert graph.dependents(B) == ()
    assert graph.leaves(A) == [B, C]
    assert graph.leaves(D) == [D]


def test_a_reference_to_a_missing_node_is_not_an_edge() -> None:
    graph = Graph([node(B, A, deps=[C])])
    assert graph.ancestors(B) == []
    assert graph.children(A) == ()
    assert graph.children(None) == ()
    assert graph.dependencies(B) == ()
    assert A not in graph.tree
    assert C not in graph.deps


def test_stays_safe_on_a_containment_cycle() -> None:

    cycle = Graph([node(A, C), node(B, A), node(C, B)])
    assert cycle.ancestors(A) == [C, B]
    assert cycle.descendants(A) == [B, C]
    assert cycle.children(None) == ()


def test_walks_a_deep_chain_without_hitting_the_recursion_limit() -> None:
    ids = [make_id(i) for i in range(5000)]
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


DONE, DROPPED, IN_PROGRESS = Status.DONE, Status.DROPPED, Status.IN_PROGRESS


def test_a_todo_node_is_blocked_until_every_dependency_is_done() -> None:
    states = Graph(
        [node(A, status=DONE), node(B, status=IN_PROGRESS), node(C, deps=[A, B]), node(D)]
    ).states
    assert states[C].blocked
    assert not states[C].ready
    assert states[C].waiting_on == (B,)
    assert states[D].ready
    assert not states[D].blocked


def test_only_todo_nodes_are_blocked_or_ready() -> None:
    states = Graph(
        [node(A), node(B, deps=[A], status=IN_PROGRESS), node(C, deps=[A], status=DONE)]
    ).states
    for node_id in (B, C):
        assert not states[node_id].blocked
        assert not states[node_id].ready
        assert states[node_id].waiting_on == (A,)


def test_a_dropped_dependency_is_flagged_and_never_unblocks() -> None:
    states = Graph([node(A, status=DROPPED), node(B, deps=[A])]).states
    assert states[B].blocked
    assert states[B].dropped_deps == (A,)
    assert states[B].waiting_on == (A,)


def test_a_child_inherits_what_its_ancestors_wait_on() -> None:
    #  A ← B (B depends on A);  C and D sit under B.
    graph = Graph([node(A), node(B, deps=[A]), node(C, B), node(D, C)])
    states = graph.states
    assert states[D].blocked
    assert states[D].waiting_on == (A,)
    assert states[D].dropped_deps == ()
    assert graph.ready_leaves() == [A]


def test_progress_counts_done_leaves_over_leaves_that_are_not_dropped() -> None:
    #  A ─ B (done), C ─ D (done), E (todo);  F (dropped) under A.
    graph = Graph(
        [
            node(A),
            node(B, A, status=DONE),
            node(C, A),
            node(D, C, status=DONE),
            node(E, C),
            node(F, A, status=DROPPED),
        ]
    )
    states = graph.states
    assert states[A].progress == Progress(2, 3)
    assert states[C].progress == Progress(1, 2)
    assert states[C].progress.fraction == 0.5
    assert states[B].progress == Progress(1, 1)
    assert states[E].progress == Progress(0, 1)
    assert states[F].progress == Progress(0, 0)
    assert states[F].progress.fraction == 0.0


def test_leaves_under_a_dropped_parent_do_not_count_and_are_not_ready() -> None:
    graph = Graph([node(A), node(B, A, status=DROPPED), node(C, B), node(D, A)])
    states = graph.states
    assert states[A].progress == Progress(0, 1)
    assert not states[C].ready
    assert graph.ready_leaves() == [D]


def test_ready_leaves_can_be_scoped_to_a_subtree() -> None:
    graph = Graph([node(A), node(B, A), node(C, A, deps=[B]), node(D), node(E, D)])
    assert graph.ready_leaves() == [B, E]
    assert graph.ready_leaves(A) == [B]
    assert graph.ready_leaves(D) == [E]


@given(graphs())
def test_states_agree_with_the_definitions(nodes: list[Node]) -> None:
    graph = Graph(nodes)
    states = graph.states
    assert states.keys() == graph.nodes.keys()
    for n in nodes:
        state = states[n.id]
        lineage = [n, *(graph[a] for a in graph.ancestors(n.id))]
        waiting = {d for x in lineage for d in x.depends_on if graph[d].status is not DONE}
        assert set(state.waiting_on) == waiting
        assert state.blocked == (n.status is Status.TODO and bool(waiting))
        assert not (state.blocked and state.ready)
        if state.ready:
            assert n.status is Status.TODO
        assert 0 <= state.progress.done <= state.progress.total <= len(graph.leaves(n.id))
