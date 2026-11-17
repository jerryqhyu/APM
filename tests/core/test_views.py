from apm.core.graph import Graph
from apm.core.model import Status
from apm.core.ops import NewNode, create_nodes, edit_body
from apm.core.project import Project, load_graph
from apm.core.views import outline, search
from tests.core.build import A, B, C, D, E, node


def test_outline_shows_state_progress_and_dependencies() -> None:
    graph = Graph(
        [
            node(A),
            node(B, A, status=Status.DONE),
            node(C, A, deps=[B]),
            node(D, A, deps=[C, E]),
            node(E, status=Status.DROPPED),
        ]
    )
    assert outline(graph).splitlines() == [
        "0000000a [work/todo·ready] node 0a (1/3)",
        "  0000000b [work/done] node 0b",
        "  0000000c [work/todo·ready] node 0c → 0000000b",
        "  0000000d [work/todo·blocked] node 0d → 0000000c, 0000000e ⚠ depends on dropped 0000000e",
        "0000000e [work/dropped] node 0e",
    ]


def test_outline_can_start_at_a_node_and_limit_depth() -> None:
    graph = Graph([node(A), node(B, A), node(C, B), node(D, C), node(E)])
    assert outline(graph, A).splitlines() == [
        "0000000b [work/todo·ready] node 0b (0/1)",
        "  0000000c [work/todo·ready] node 0c (0/1)",
        "    0000000d [work/todo·ready] node 0d",
    ]
    assert outline(graph, depth=1).splitlines() == [
        "0000000a [work/todo·ready] node 0a (0/1) [+3 below]",
        "0000000e [work/todo·ready] node 0e",
    ]
    assert outline(Graph([])) == ""


def test_search_matches_titles_and_bodies(project: Project) -> None:
    stream, parse, other = create_nodes(
        project,
        [NewNode(title="Stream agent output"), NewNode(title="Parse"), NewNode(title="Other")],
    ).created
    edit_body(project, parse, "## Intent\n\nRead the agent OUTPUT stream line by line.\n")
    graph = load_graph(project)

    hits = search(project, graph, "output  AGENT")
    assert [h.node_id for h in hits] == [stream, parse]  # the title match first
    assert hits[0].snippet == ""
    assert hits[1].snippet == "Read the agent OUTPUT stream line by line."
    assert search(project, graph, "nothing here") == []
    assert search(project, graph, "   ") == []
    assert [h.node_id for h in search(project, graph, "other")] == [other]
