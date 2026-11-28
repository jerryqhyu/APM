from dataclasses import replace
from pathlib import Path

from apm.core.config import ApmConfig
from apm.core.graph import Graph
from apm.core.model import Delivery, Kind, Node, Status
from apm.core.store import ReviewVerdict, Store, TaskReview, content_fingerprint
from apm.core.task_review import due_task_reviews, implement_blocker, ready_gates, review_gate
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


def in_feature(task: Node) -> Graph:
    return Graph([node(A), task])


def test_a_feature_not_broken_down_yet_is_not_reviewed_on_its_own(tmp_path: Path) -> None:
    store = Store(tmp_path)
    graph = Graph([node(A), node(B, parent=A), node(C)])  # C: a backlog feature, nothing inside
    assert due_task_reviews(graph, store, body) == [B]
    assert "not broken down" in (implement_blocker(graph, store, config(), C, body) or "")
    review(store, graph[C], "pass")  # a review started by hand opens the gate as for a task
    assert implement_blocker(graph, store, config(), C, body) is None
    assert implement_blocker(graph, store, config(), C, lambda _: "edited") is not None


def test_one_error_is_retried_once_per_content(tmp_path: Path) -> None:
    task = node(B, parent=A)
    store = Store(tmp_path)
    review(store, task, "error")
    assert due_task_reviews(in_feature(task), store, body) == [B]
    review(store, task, "error")
    assert due_task_reviews(in_feature(task), store, body) == []
    edited = replace(task, title="edited")
    assert due_task_reviews(in_feature(edited), store, body) == [B]


def test_content_changes_make_a_task_due_status_and_delivery_do_not(tmp_path: Path) -> None:
    task = node(B, parent=A)
    store = Store(tmp_path)
    review(store, replace(task, status=Status.IN_PROGRESS), "pass")  # back to To do since
    unchanged = [
        task,
        replace(task, delivery=Delivery(repo="r", branch="b")),
        replace(task, delivery=Delivery(repo="r", commits=("abc",), pr="https://pr")),
    ]
    for same in unchanged:
        assert due_task_reviews(in_feature(same), store, body) == []
    for changed in [replace(task, title="new title"), replace(task, kind=Kind.DECISION)]:
        assert due_task_reviews(in_feature(changed), store, body) == [B]
    assert due_task_reviews(in_feature(task), store, lambda _: "new description") == [B]


def config(task_review: bool = True) -> ApmConfig:
    return ApmConfig.model_validate(
        {
            "name": "p",
            "code": {"repos": {"app": {"path": ".."}}},
            "automation": {"task_review": task_review},
        }
    )


def test_implement_is_gated_on_a_passing_review_of_todo_tasks_only(tmp_path: Path) -> None:
    store = Store(tmp_path)
    graph = Graph([node(B), node(E, status=Status.IN_PROGRESS), node(F, status=Status.DONE)])
    assert implement_blocker(graph, store, config(), B, body) is not None
    assert implement_blocker(graph, store, config(), E, body) is None
    assert implement_blocker(graph, store, config(), F, body) is None
    review(store, graph[B], "too_big")
    assert "too_big" in (implement_blocker(graph, store, config(), B, body) or "")
    review(store, graph[B], "pass")
    assert implement_blocker(graph, store, config(), B, body) is None
    assert implement_blocker(graph, store, config(), B, lambda _: "edited") is not None


def test_implement_is_gated_the_same_when_task_review_is_off(tmp_path: Path) -> None:
    store, off = Store(tmp_path), config(task_review=False)
    graph = Graph([node(B)])
    for verdict in [None, "unclear", "pass"]:
        if verdict:
            review(store, graph[B], verdict)
        expected = implement_blocker(graph, store, config(), B, body)
        assert implement_blocker(graph, store, off, B, body) == expected
    assert implement_blocker(graph, store, off, B, lambda _: "edited") is not None


def test_a_ready_task_without_a_passing_review_is_held_back(tmp_path: Path) -> None:
    store = Store(tmp_path)
    graph = Graph(
        [
            node(A),
            node(B, parent=A),
            node(C, parent=A),
            node(D, parent=A, deps=[B]),  # blocked: not ready, so not held back here
            node(E),  # top level, nothing inside: a feature not broken down yet
        ]
    )
    assert ready_gates(graph, store, body) == {B: "unreviewed", C: "unreviewed", E: "breakdown"}
    review(store, graph[B], "pass")
    review(store, graph[C], "error")
    assert ready_gates(graph, store, body) == {C: "error", E: "breakdown"}
    assert ready_gates(graph, store, body, only=B) == {}
    assert review_gate(graph, store, B, lambda _: "edited") == "unreviewed"  # changed since
    assert "says error" in (implement_blocker(graph, store, config(), C, body) or "")
