from hypothesis import given

from apm.core.graph import Graph
from apm.core.lifting import LevelView, LiftedEdge, Port, check_all, entanglements, level_view
from apm.core.model import Node, Status
from tests.core.build import A, B, C, D, E, F, G, H, node
from tests.strategies import graphs

#  A              B          H
#  ├─ C           ├─ F
#  │  └─ E        └─ G
#  └─ D
#
#  E → F   (E depends on F: two levels down under A, one level down under B)
#  D → G
#  E → D   (inside A)
#  C → H   (C is under A; H is top-level)
FIXTURE = Graph(
    [
        node(A),
        node(B),
        node(C, A, deps=[H]),
        node(D, A, deps=[G]),
        node(E, C, deps=[D, F]),
        node(F, B),
        node(G, B),
        node(H),
    ]
)


def test_the_top_level_lifts_deep_edges_and_merges_duplicates() -> None:
    view = level_view(FIXTURE)
    assert view.root is None
    assert view.nodes == (A, B, H)
    assert view.edges == (
        LiftedEdge(A, B, ((D, G), (E, F))),
        LiftedEdge(A, H, ((C, H),)),
    )
    assert view.ports == ()
    assert view.internal == ((E, D),)
    assert view.entangled == ()


def test_inside_a_node_edges_that_leave_it_become_ports() -> None:
    view = level_view(FIXTURE, A)
    assert view.nodes == (C, D)
    assert view.edges == (LiftedEdge(C, D, ((E, D),)),)
    assert view.ports == (
        Port(C, F, "out", ((E, F),)),
        Port(C, H, "out", ((C, H),)),
        Port(D, G, "out", ((D, G),)),
    )
    assert view.internal == ()


def test_incoming_edges_are_ports_too() -> None:
    view = level_view(FIXTURE, B)
    assert view.nodes == (F, G)
    assert view.edges == ()
    assert view.ports == (Port(F, E, "in", ((E, F),)), Port(G, D, "in", ((D, G),)))


def test_a_leaf_has_an_empty_level() -> None:
    assert level_view(FIXTURE, H) == LevelView(H, (), (), (), (), ())


def test_entanglement_is_a_cycle_of_lifted_edges() -> None:
    #  A ─ C, D     B ─ E, F      C → E and F → D: no stored cycle, but A ⇄ B at the top level.
    graph = Graph(
        [node(A), node(B), node(C, A, deps=[E]), node(D, A), node(E, B), node(F, B, deps=[D])]
    )
    assert level_view(graph).entangled == ((A, B),)
    assert level_view(graph, A).entangled == ()
    issues = entanglements(graph)
    assert [(i.code, i.nodes) for i in issues] == [("I5", (A, B))]
    assert issues[0].message == "entanglement at the top level: 0000000a, 0000000b"


def test_entanglement_below_the_top_level_names_its_parent() -> None:
    #  Under A: B ─ D, E and C ─ F, G, with D → F and G → E.
    graph = Graph(
        [
            node(A),
            node(B, A),
            node(C, A),
            node(D, B, deps=[F]),
            node(E, B),
            node(F, C),
            node(G, C, deps=[E]),
        ]
    )
    assert [i.message for i in entanglements(graph)] == [
        "entanglement under 0000000a: 0000000b, 0000000c"
    ]


def test_check_all_adds_entanglement_to_the_warnings() -> None:
    graph = Graph(
        [
            node(A, status=Status.DONE),
            node(B),
            node(C, A, deps=[E]),
            node(D, A),
            node(E, B),
            node(F, B, deps=[D]),
        ]
    )
    report = check_all(graph)
    assert report.ok
    # I9: built by hand, the parents have no review steps (mutate would have added them).
    assert [i.code for i in report.warnings] == ["I6", "I9", "I5"]


def test_check_all_stops_at_errors() -> None:
    report = check_all(Graph([node(A, deps=[A])]))
    assert [i.code for i in report.errors] == ["I2"]
    assert report.warnings == ()


@given(graphs())
def test_every_edge_is_accounted_for_exactly_once_at_each_level(nodes: list[Node]) -> None:
    graph = Graph(nodes)
    for root in (None, *graph.nodes):
        view = level_view(graph, root)
        inside = set(graph.nodes) if root is None else set(graph.descendants(root))
        touching = sorted((u, v) for u, v in graph.deps.edges if u in inside or v in inside)
        shown = [
            *(e for edge in view.edges for e in edge.underlying),
            *(e for port in view.ports for e in port.underlying),
            *view.internal,
        ]
        assert sorted(shown) == touching

        for edge in view.edges:
            assert edge.src != edge.dst
            assert {edge.src, edge.dst} <= set(view.nodes)
        for port in view.ports:
            assert port.node in view.nodes
            assert port.outside not in inside
        if root is None:
            assert view.ports == ()


@given(graphs())
def test_lifted_edges_match_a_naive_recomputation(nodes: list[Node]) -> None:
    graph = Graph(nodes)

    def owner(node_id: str, root: str | None) -> str | None:
        """The child of `root` that contains `node_id`, by walking up from it."""
        for candidate in (node_id, *graph.ancestors(node_id)):
            if graph[candidate].parent == root:
                return candidate
        return None

    for root in (None, *graph.nodes):
        expected = set()
        for n in nodes:
            for dep in n.depends_on:
                a, b = owner(n.id, root), owner(dep, root)
                if a is not None and b is not None and a != b:
                    expected.add((a, b))
        assert {(e.src, e.dst) for e in level_view(graph, root).edges} == expected
