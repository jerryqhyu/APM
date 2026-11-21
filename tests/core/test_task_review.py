from dataclasses import replace
from pathlib import Path

from apm.core.graph import Graph
from apm.core.model import Delivery, Kind, Node, Status
from apm.core.store import ReviewVerdict, Store, TaskReview, content_fingerprint
from apm.core.task_review import due_task_reviews
from tests.core.build import A, B, C, D, E, F, G, node

BODIES = {A: "feature", B: "task b", C: "task c"}


def body(node_id: str) -> str:
    return BODIES.get(node_id, "")


def review(store: Store, n: Node, verdict: ReviewVerdict) -> None:
    store.add_task_review(TaskReview(n.id, content_fingerprint(n, body(n.id)), verdict, {}))


def test_only_todo_tasks_without_a_review_are_due(tmp_path: Path) -> None:
    graph = Graph(
        [
            node(A),  # a feature: has children
            node(B, parent=A),
            node(C, parent=A),
            node(D, parent=A, kind=Kind.REVIEW),
            node(E, status=Status.IN_PROGRESS),
            node(F, status=Status.DONE),
            node(G, status=Status.DROPPED),
        ]
    )
    store = Store(tmp_path)
    assert due_task_reviews(graph, store, body) == [B, C]
    review(store, graph[C], "pass")
    assert due_task_reviews(graph, store, body) == [B]
    review(store, graph[B], "too_big")
    assert due_task_reviews(graph, store, body) == []


def test_one_error_is_retried_once_per_content(tmp_path: Path) -> None:
    task = node(B)
    store = Store(tmp_path)
    review(store, task, "error")
    assert due_task_reviews(Graph([task]), store, body) == [B]
    review(store, task, "error")
    assert due_task_reviews(Graph([task]), store, body) == []
    edited = replace(task, title="edited")
    assert due_task_reviews(Graph([edited]), store, body) == [B]


def test_content_changes_make_a_task_due_status_and_delivery_do_not(tmp_path: Path) -> None:
    task = node(B)
    store = Store(tmp_path)
    review(store, replace(task, status=Status.IN_PROGRESS), "pass")  # back to To do since
    unchanged = [
        task,
        replace(task, delivery=Delivery(repo="r", branch="b")),
        replace(task, delivery=Delivery(repo="r", commits=("abc",), pr="https://pr")),
    ]
    for same in unchanged:
        assert due_task_reviews(Graph([same]), store, body) == []
    for changed in [replace(task, title="new title"), replace(task, kind=Kind.DECISION)]:
        assert due_task_reviews(Graph([changed]), store, body) == [B]
    assert due_task_reviews(Graph([task]), store, lambda _: "new description") == [B]
