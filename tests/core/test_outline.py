from apm.core.graph import Graph
from apm.core.model import Status
from apm.core.outline import format_outline
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
    assert format_outline(graph).splitlines() == [
        "0000000a [work/todo·ready] node 0a (1/3)",
        "  0000000b [work/done] node 0b",
        "  0000000c [work/todo·ready] node 0c → 0000000b",
        "  0000000d [work/todo·blocked] node 0d → 0000000c, 0000000e ⚠ depends on dropped 0000000e",
        "0000000e [work/dropped] node 0e",
    ]


def test_outline_can_start_at_a_node_and_limit_depth() -> None:
    graph = Graph([node(A), node(B, A), node(C, B), node(D, C), node(E)])
    assert format_outline(graph, A).splitlines() == [
        "0000000b [work/todo·ready] node 0b (0/1)",
        "  0000000c [work/todo·ready] node 0c (0/1)",
        "    0000000d [work/todo·ready] node 0d",
    ]
    assert format_outline(graph, depth=1).splitlines() == [
        "0000000a [work/todo·ready] node 0a (0/1) [+3 below]",
        "0000000e [work/todo·ready] node 0e",
    ]
    assert format_outline(Graph([])) == ""
