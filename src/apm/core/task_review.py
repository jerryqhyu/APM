"""Which tasks need a task review.

A task is a leaf that is not a review step. While it is To do, every version of its content
(title, kind and body, see `content_fingerprint`) gets one review. A review that errored is
retried automatically once for the same content; after a second error the task waits for an
edit or a review started by hand. `due_task_reviews` is the one place that decides this, for
every entry point that starts reviews on its own.
"""

from collections.abc import Callable
from typing import Any

from apm.core.config import ApmConfig
from apm.core.graph import Graph
from apm.core.model import NodeId, Status
from apm.core.store import Store, content_fingerprint

MAX_ERRORS = 2
"""Automatic reviews of one version of a task's content that may end in `error`."""


def needs_review(store: Store, node_id: NodeId, fingerprint: str) -> bool:
    latest = store.latest_task_review(node_id)
    if latest is not None and latest.fingerprint == fingerprint and latest.verdict != "error":
        return False
    return store.count_errors(node_id, fingerprint) < MAX_ERRORS


def due_task_reviews(graph: Graph, store: Store, body: Callable[[NodeId], str]) -> list[NodeId]:
    """The To do tasks whose current content has no review yet, in id order.

    `body` gives a node's description (e.g. `lambda n: read_body(apm_dir, n)`).
    """
    return [
        node_id
        for node_id in sorted(graph.nodes)
        if graph.is_leaf(node_id)
        and graph[node_id].status == Status.TODO
        and not graph.is_review(node_id)
        and needs_review(store, node_id, content_fingerprint(graph[node_id], body(node_id)))
    ]


def implement_blocker(
    graph: Graph, store: Store, config: ApmConfig, node_id: NodeId, body: Callable[[NodeId], str]
) -> str | None:
    """Why Implement may not start on this node now, or None: a To do task needs a passing
    review of the content it has now. Every entry point (UI, API, CLI, MCP, automation) goes
    through this, by way of `check_startable`. The gate holds whatever `config` says:
    `automation.task_review` only switches off the reviews `apm serve` starts on its own.
    """
    if graph[node_id].status != Status.TODO:
        return None
    latest = store.latest_task_review(node_id)
    if latest is None or latest.fingerprint != content_fingerprint(graph[node_id], body(node_id)):
        return "this task's current content has not been reviewed: run Review task first"
    if latest.verdict != "pass":
        return f"this task's latest review says {latest.verdict}: fix it and review it again"
    return None


def review_state(
    graph: Graph, store: Store, node_id: NodeId, body: str, running: bool
) -> dict[str, Any]:
    """What the UI shows of a task's review: `state` (none, running, pass, too_big, unclear,
    error, or stale when the content changed since), plus what the reviewer returned.
    """
    latest = store.latest_task_review(node_id)
    if running or latest is None:
        return {"state": "running" if running else "none"}
    stale = latest.fingerprint != content_fingerprint(graph[node_id], body)
    return {**latest.data, "state": "stale" if stale else latest.verdict, "verdict": latest.verdict}
