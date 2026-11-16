from hypothesis import given

from apm.core.graph import Graph
from apm.core.model import Node, Status
from apm.core.status import Progress, compute_states, ready_leaves
from tests.core.build import A, B, C, D, E, F, node
from tests.strategies import graphs

DONE, DROPPED, IN_PROGRESS = Status.DONE, Status.DROPPED, Status.IN_PROGRESS


def test_a_todo_node_is_blocked_until_every_dependency_is_done() -> None:
    states = compute_states(
        Graph([node(A, status=DONE), node(B, status=IN_PROGRESS), node(C, deps=[A, B]), node(D)])
    )
    assert states[C].blocked
    assert not states[C].ready
    assert states[C].waiting_on == (B,)
    assert states[D].ready
    assert not states[D].blocked


def test_only_todo_nodes_are_blocked_or_ready() -> None:
    states = compute_states(
        Graph([node(A), node(B, deps=[A], status=IN_PROGRESS), node(C, deps=[A], status=DONE)])
    )
    for node_id in (B, C):
        assert not states[node_id].blocked
        assert not states[node_id].ready
        assert states[node_id].waiting_on == (A,)


def test_a_dropped_dependency_is_flagged_and_never_unblocks() -> None:
    states = compute_states(Graph([node(A, status=DROPPED), node(B, deps=[A])]))
    assert states[B].blocked
    assert states[B].dropped_deps == (A,)
    assert states[B].waiting_on == (A,)


def test_a_child_inherits_what_its_ancestors_wait_on() -> None:
    #  A ← B (B depends on A);  C and D sit under B.
    graph = Graph([node(A), node(B, deps=[A]), node(C, B), node(D, C)])
    states = compute_states(graph)
    assert states[D].blocked
    assert states[D].waiting_on == (A,)
    assert states[D].dropped_deps == ()
    assert ready_leaves(graph) == [A]


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
    states = compute_states(graph)
    assert states[A].progress == Progress(2, 3)
    assert states[C].progress == Progress(1, 2)
    assert states[C].progress.fraction == 0.5
    assert states[B].progress == Progress(1, 1)
    assert states[E].progress == Progress(0, 1)
    assert states[F].progress == Progress(0, 0)
    assert states[F].progress.fraction == 0.0


def test_leaves_under_a_dropped_parent_do_not_count_and_are_not_ready() -> None:
    graph = Graph([node(A), node(B, A, status=DROPPED), node(C, B), node(D, A)])
    states = compute_states(graph)
    assert states[A].progress == Progress(0, 1)
    assert not states[C].ready
    assert ready_leaves(graph) == [D]


def test_ready_leaves_can_be_scoped_to_a_subtree() -> None:
    graph = Graph([node(A), node(B, A), node(C, A, deps=[B]), node(D), node(E, D)])
    assert ready_leaves(graph) == [B, E]
    assert ready_leaves(graph, A) == [B]
    assert ready_leaves(graph, D) == [E]


@given(graphs())
def test_states_agree_with_the_definitions(nodes: list[Node]) -> None:
    graph = Graph(nodes)
    states = compute_states(graph)
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
