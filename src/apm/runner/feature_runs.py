"""Review all tasks and Implement all: one agent on every task of a feature at once.

Both cover the tasks inside a feature at any depth, nested features included, but not review
steps. On a task they cover that one task. What may start goes through a `RunQueue`, so asking
for more than `runner.max_concurrent` never fails: the rest wait their turn. Each says what it
started, what waits for room, and what it skipped and why.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from apm.core.config import read_config
from apm.core.files import read_body
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import NodeId, Status
from apm.core.project import load_graph
from apm.core.review_lock import under_review
from apm.core.store import Run
from apm.core.task_review import implement_blocker
from apm.runner.agents import check_mode
from apm.runner.queue import Queued, RunQueue

TASK_REVIEWER = "apm-task-reviewer"
IMPLEMENTER = "apm-implementer"


@dataclass(frozen=True, slots=True)
class Skipped:
    node_id: NodeId
    reason: str


@dataclass(frozen=True, slots=True)
class Batch:
    """What a feature-level start did."""

    started: tuple[Run, ...] = ()
    waiting: tuple[NodeId, ...] = ()
    """Queued until there is room under `runner.max_concurrent`."""
    skipped: tuple[Skipped, ...] = ()


def review_all_tasks(
    queue: RunQueue, node_id: NodeId, *, mode: str | None = None, message: str = ""
) -> Batch:
    """Starts a task review on every To do task in the feature, skipping those already under
    review. A task that has passed is reviewed again: that is how tasks are checked against a
    task size limit that changed since.
    """
    graph = load_graph(queue.runner.project)
    tasks, skipped = reviewable(graph, node_id, partial(under_review, queue.runner.project.apm_dir))
    return _submit(queue, TASK_REVIEWER, tasks, skipped, mode=mode, message=message)


def implement_feature(
    queue: RunQueue, node_id: NodeId, *, mode: str | None = None, message: str = ""
) -> Batch:
    """Implement all: starts the implementer on every task of the feature that is ready now,
    stacked ones included, and that passes the Implement gate (a passing task review of its
    current content). Blocked tasks, and those that fail the gate, are skipped with why.

    Eager mode plugs in here. When `feature_settings(config, graph, node_id).eager` is on, a
    feature is to be built in one run on one branch and then split into one pull request per task,
    so this is where that run starts instead of one implementer per task. Eager mode isn't built
    yet: every feature is built task by task.
    """
    project, store = queue.runner.project, queue.runner.store
    graph, config = load_graph(project), read_config(project.apm_dir)
    body, locked = partial(read_body, project.apm_dir), partial(under_review, project.apm_dir)
    gate = partial(implement_blocker, graph, store, config, body=body)
    tasks, skipped = implementable(graph, node_id, locked, gate)
    return _submit(queue, IMPLEMENTER, tasks, skipped, mode=mode, message=message)


# --- which tasks ---


def tasks_in(graph: Graph, node_id: NodeId) -> list[NodeId]:
    """The tasks a feature-level action covers: the To do leaves under `node_id` at any depth that
    aren't review steps, in outline order. On a task, that task.
    """
    return [
        leaf
        for leaf in graph.leaves(node_id)
        if not graph.is_review(leaf) and graph[leaf].status is Status.TODO
    ]


def reviewable(
    graph: Graph, node_id: NodeId, locked: Callable[[NodeId], bool]
) -> tuple[list[NodeId], list[Skipped]]:
    """The tasks Review all tasks starts, and those it skips. `locked` says whether a task is
    under review.
    """
    tasks: list[NodeId] = []
    skipped: list[Skipped] = []
    for task in tasks_in(graph, node_id):
        if locked(task):
            skipped.append(Skipped(task, "already under review"))
        elif _under_dropped(graph, task):
            skipped.append(Skipped(task, "inside a dropped node"))
        else:
            tasks.append(task)
    return tasks, skipped


def implementable(
    graph: Graph,
    node_id: NodeId,
    locked: Callable[[NodeId], bool],
    gate: Callable[[NodeId], str | None],
) -> tuple[list[NodeId], list[Skipped]]:
    """The tasks Implement all starts, and those it skips. `gate` is `implement_blocker` with the
    project's store, config and bodies bound.
    """
    tasks: list[NodeId] = []
    skipped: list[Skipped] = []
    for task in tasks_in(graph, node_id):
        state = graph.states[task]
        if not state.ready:
            # What it can't stack on; or all of it, when it waits on separate unmerged branches.
            blocking = [n for n in state.waiting_on if n not in state.stacked_on]
            waits = ", ".join(handle(n) for n in blocking or state.waiting_on)
            reason = f"blocked: waiting on {waits}" if waits else "inside a dropped node"
            skipped.append(Skipped(task, reason))
        elif locked(task):
            skipped.append(Skipped(task, "under review: wait for its review to finish"))
        elif reason := gate(task):
            skipped.append(Skipped(task, reason))
        else:
            tasks.append(task)
    return tasks, skipped


def _under_dropped(graph: Graph, node_id: NodeId) -> bool:
    return any(graph[a].status is Status.DROPPED for a in graph.ancestors(node_id))


def _submit(
    queue: RunQueue,
    agent: str,
    tasks: list[NodeId],
    skipped: list[Skipped],
    *,
    mode: str | None,
    message: str,
) -> Batch:
    """Queues `agent` on `tasks` and starts what fits. Reports on these tasks only: the queue may
    also start runs asked for earlier.
    """
    check_mode(mode, read_config(queue.runner.project.apm_dir))  # refuse a bad mode before any run
    queue.add(Queued(agent, task, message, mode) for task in tasks)
    pumped = queue.pump()
    mine = set(tasks)
    started = tuple(r for r in pumped.started if r.agent == agent and r.node_id in mine)
    dropped = [
        Skipped(d.item.node_id, d.reason)
        for d in pumped.dropped
        if d.item.agent == agent and d.item.node_id in mine
    ]
    waiting = tuple(q.node_id for q in queue.waiting if q.agent == agent and q.node_id in mine)
    return Batch(started, waiting, (*skipped, *dropped))
