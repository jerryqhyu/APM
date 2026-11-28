"""Which tasks need a task review.

A task is a leaf inside a feature that is not a review step. While it is To do, every version of
its content (title, kind and body, see `content_fingerprint`) gets one review. A review that
errored is retried automatically once for the same content; after a second error the task waits
for an edit or a review started by hand. `due_task_reviews` is the one place that decides this,
for every entry point that starts reviews on its own.

A top-level node with nothing inside it is a feature waiting for its breakdown, not a task
(`awaits_breakdown`). It is never reviewed on its own, so you can draft and discuss it freely;
Implement on it still waits for a review you start by hand.
"""

from collections.abc import Callable
from typing import Any, Literal

from apm.core.config import ApmConfig
from apm.core.graph import Graph
from apm.core.model import Node, NodeId, Status
from apm.core.store import Store, content_fingerprint

MAX_ERRORS = 2
"""Automatic reviews of one version of a task's content that may end in `error`."""


def needs_review(store: Store, node_id: NodeId, fingerprint: str) -> bool:
    latest = store.latest_task_review(node_id)
    if latest is not None and latest.fingerprint == fingerprint and latest.verdict != "error":
        return False
    return store.count_errors(node_id, fingerprint) < MAX_ERRORS


def awaits_breakdown(graph: Graph, node_id: NodeId) -> bool:
    """A top-level node with nothing inside it: a feature not broken down yet, as the List view
    shows it, rather than a task.
    """
    return graph[node_id].parent is None and graph.is_leaf(node_id)


def due_task_reviews(graph: Graph, store: Store, body: Callable[[NodeId], str]) -> list[NodeId]:
    """The To do tasks whose current content has no review yet, in id order.

    `body` gives a node's description (e.g. `lambda n: read_body(apm_dir, n)`).
    """
    return [
        node_id
        for node_id in sorted(graph.nodes)
        if graph.is_leaf(node_id)
        and not awaits_breakdown(graph, node_id)
        and graph[node_id].status == Status.TODO
        and not graph.is_review(node_id)
        and needs_review(store, node_id, content_fingerprint(graph[node_id], body(node_id)))
    ]


type Gate = Literal["breakdown", "unreviewed", "too_big", "unclear", "error"]
"""Why a To do task can't be built yet, as far as its review goes: a top-level node not broken
down, content with no review (or changed since), or a review that didn't pass."""


GATE_NOTE: dict[str, str] = {
    "breakdown": "not broken down yet",
    "unreviewed": "not reviewed",
    "too_big": "its review found it too big",
    "unclear": "its review found it unclear",
    "error": "its review failed",
}
"""Each gate in a few words, for `apm ready` and `apm_ready`."""


def review_gate(
    graph: Graph, store: Store, node_id: NodeId, body: Callable[[NodeId], str]
) -> Gate | None:
    """What stands between a To do task and Implement in its review, or None if it passed for
    its current content (or isn't To do). The UI shows such a task as not reviewed, not Ready.
    """
    if graph[node_id].status != Status.TODO:
        return None
    latest = store.latest_task_review(node_id)
    if latest is None and awaits_breakdown(graph, node_id):
        return "breakdown"
    if latest is None or latest.fingerprint != content_fingerprint(graph[node_id], body(node_id)):
        return "unreviewed"
    if latest.verdict == "pass":
        return None
    return latest.verdict


def implement_blocker(
    graph: Graph, store: Store, config: ApmConfig, node_id: NodeId, body: Callable[[NodeId], str]
) -> str | None:
    """Why Implement may not start on this node now, or None: a To do task needs a passing
    review of the content it has now. Every entry point (UI, API, CLI, MCP, automation) goes
    through this, by way of `check_startable`. The gate holds whatever `config` says:
    `automation.task_review` only switches off the reviews `apm serve` starts on its own.
    """
    gate = review_gate(graph, store, node_id, body)
    if gate is None:
        return None
    if gate == "breakdown":
        return (
            "this feature is not broken down yet: Decompose it, "
            "or run Review task to build it as one task"
        )
    if gate == "unreviewed":
        return "this task's current content has not been reviewed: run Review task first"
    return f"this task's latest review says {gate}: fix it and review it again"


def ready_gates(
    graph: Graph, store: Store, body: Callable[[NodeId], str], *, only: NodeId | None = None
) -> dict[NodeId, Gate]:
    """The ready leaves (review steps aside) that their review keeps from being built: shown as
    not reviewed rather than Ready, and left out of what is ready to build. `only` looks at one.
    """
    gates: dict[NodeId, Gate] = {}
    for node_id in graph.nodes if only is None else [only]:
        if (
            not graph.states[node_id].ready
            or not graph.is_leaf(node_id)
            or graph.is_review(node_id)
        ):
            continue
        if (gate := review_gate(graph, store, node_id, body)) is not None:
            gates[node_id] = gate
    return gates


def review_state(
    graph: Graph, store: Store, node_id: NodeId, body: str, running: bool
) -> dict[str, Any]:
    """What the UI shows of a task's review: `state` (none, running, pass, too_big, unclear,
    error, or stale when the content changed since), plus what the reviewer returned and when. A
    stale review also says what changed (`changes`), when the review kept what it was shown.
    """
    latest = store.latest_task_review(node_id)
    if running or latest is None:
        return {"state": "running" if running else "none"}
    stale = latest.fingerprint != content_fingerprint(graph[node_id], body)
    shown = {key: value for key, value in latest.data.items() if key != "reviewed"}
    out = {
        **shown,
        "state": "stale" if stale else latest.verdict,
        "verdict": latest.verdict,
        "reviewed_at": latest.created,
    }
    if stale:
        out["changes"] = review_changes(latest.data.get("reviewed"), graph[node_id], body)
    return out


def review_changes(reviewed: object, node: Node, body: str) -> list[str] | None:
    """What changed since a review, in a few words each: `the title`, `Approach edited`,
    `Out of scope added`. None if the review didn't keep what it was shown (an older one).
    """
    if not isinstance(reviewed, dict):
        return None
    changes = []
    if reviewed.get("title") != node.title:
        changes.append("the title")
    if reviewed.get("kind") != str(node.kind):
        changes.append("the kind")
    before, after = _sections(str(reviewed.get("body", ""))), _sections(body)
    for heading in dict.fromkeys([*before, *after]):
        name = heading or "the opening text"
        if heading not in before:
            changes.append(f"{name} added")
        elif heading not in after:
            changes.append(f"{name} removed")
        elif before[heading] != after[heading]:
            changes.append(f"{name} edited")
    return changes


def _sections(body: str) -> dict[str, str]:
    """A description by its `## ` sections, the text before the first one under ''."""
    sections: dict[str, list[str]] = {"": []}
    current = ""
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        else:
            sections[current].append(line)
    texts = {heading: "\n".join(lines).strip() for heading, lines in sections.items()}
    if not texts[""]:
        del texts[""]
    return texts
