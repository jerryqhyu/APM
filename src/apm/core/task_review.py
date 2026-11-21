"""Which tasks need a task review.

A task is a leaf that is not a review step. While it is To do, every version of its content
(title, kind and body, see `content_fingerprint`) gets one review. A review that errored is
retried automatically once for the same content; after a second error the task waits for an
edit or a review started by hand. `due_task_reviews` is the one place that decides this, for
every entry point that starts reviews on its own.
"""

from collections.abc import Callable

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
