from apm.core.graph import Graph
from apm.core.model import Delivery, Status
from apm.core.ops import NewNode, create_nodes, edit_body
from apm.core.project import Project, load_graph
from apm.core.views import node_view, outline, pr_label, search
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


def test_stacked_work_is_marked_with_what_it_stacks_on() -> None:
    first = Delivery(repo="app", branch="apm/a", pr="https://github.com/o/r/pull/20")
    second = Delivery(repo="app", branch="apm/b", pr="https://github.com/o/r/pull/21")
    graph = Graph(
        [
            node(A, status=Status.IN_PROGRESS, delivery=first),
            node(B, deps=[A], status=Status.IN_PROGRESS, delivery=second),
            node(C, deps=[A, B]),
        ]
    )
    assert outline(graph).splitlines()[2] == (
        "0000000c [work/todo·ready·stacked] node 0c → 0000000a, 0000000b"
        " · stacked on 0000000b (#21)"
    )
    view = node_view(graph, C)
    assert (view["ready"], view["blocked"], view["stack_top"]) == (True, False, B)
    assert view["stacked_on"] == [
        {"id": A, "handle": "0000000a", "pr": "#20"},
        {"id": B, "handle": "0000000b", "pr": "#21"},
    ]
    assert (node_view(graph, A)["stacked_on"], node_view(graph, A)["stack_top"]) == ([], None)


def test_pr_label_is_the_number_when_there_is_one() -> None:
    assert pr_label("https://github.com/o/r/pull/21") == "#21"
    assert (
        pr_label("https://example.com/merge-requests/x") == "https://example.com/merge-requests/x"
    )
    assert pr_label(None) is None


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
