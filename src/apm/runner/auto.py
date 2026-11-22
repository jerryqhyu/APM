"""The runs `apm serve` starts on its own.

Automations push `(agent, node, version)` onto one `AutoQueue`. A push whose version (what the
run would work on, e.g. a task's content fingerprint) changed restarts that run's wait, so a burst
of edits ends in one run on the final content. `pump` starts the runs whose wait is over while
there is room under `runner.max_concurrent`; a run that can't start yet stays queued. One version
gets one automatic run per server: a run that errored is tried again after an edit or a restart,
except a task review, which `due_task_reviews` retries once for the same content (`MAX_ERRORS`).

It also reconciles (retargeting open PRs) every `reconcile_interval_s`, and queues a feature
review whenever a review step becomes ready, again after its follow-ups are done. The follow-ups
that review proposed (and APM accepted) are built once their task review passes. After each
reconcile it looks for task pull requests of eager features that changed since their split, and
with `automation.eager_redo` queues a redo for each change (once per change).

Runs you asked for that are waiting for room (`manual`, a `RunQueue`) start first.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from apm.core.config import read_config
from apm.core.eager import RedoPlan, detect_redos
from apm.core.errors import ApmError
from apm.core.files import read_body
from apm.core.graph import Graph
from apm.core.model import Kind, NodeId
from apm.core.project import load_graph
from apm.core.reconcile import reconcile
from apm.core.store import Run, Store, content_fingerprint
from apm.core.task_review import due_task_reviews
from apm.runner.queue import RunQueue
from apm.runner.runner import FEATURE_REVIEWER, Runner, RunnerBusy

TASK_REVIEWER = "apm-task-reviewer"
IMPLEMENTER = "apm-implementer"
EAGER_REDO = "eager-redo"
"""The queue's name for an automatic redo of an eager feature (`apm.runner.eager`)."""
DEBOUNCE_S = 3.0
"""How long a run waits after the last change to what it works on."""
RESCAN_ON = ("graph.changed", "run.finished")
"""Events after which `drive` scans again: a finished run may have left a review to retry."""


@dataclass
class AutoQueue:
    runner: Runner
    debounce_s: float = DEBOUNCE_S
    clock: Callable[[], float] = time.monotonic
    queued: dict[tuple[str, NodeId], tuple[str, float]] = field(default_factory=dict)
    """(agent, node) → (version, when it may start)."""
    started: dict[tuple[str, NodeId], str] = field(default_factory=dict)
    """(agent, node) → the version last started."""
    task_runs: dict[NodeId, str] = field(default_factory=dict)
    """task → the run of its last automatic task review."""
    next_reconcile: float = 0.0
    """When the next periodic reconcile is due (0 = at once)."""
    manual: RunQueue = field(init=False)
    """Runs asked for by hand (Review all tasks, Implement all) waiting for room. They go first."""
    redos: dict[NodeId, RedoPlan] = field(default_factory=dict)
    """Eager features whose task PRs changed, as the last reconcile found them."""

    def __post_init__(self) -> None:
        self.manual = RunQueue(self.runner)

    def push(self, agent: str, node_id: NodeId, version: str) -> None:
        key = (agent, node_id)
        if version not in (self.started.get(key), self.queued.get(key, ("",))[0]):
            self.queued[key] = (version, self.clock() + self.debounce_s)

    def reconcile(self) -> bool:
        """Reconciles, retargeting open PRs, once `reconcile_interval_s` has passed since the
        last time. True if it ran.
        """
        automation = read_config(self.runner.project.apm_dir).automation
        if not automation.reconcile or self.clock() < self.next_reconcile:
            return False
        self.next_reconcile = self.clock() + automation.reconcile_interval_s
        with contextlib.suppress(Exception):  # git or gh failed: try again next interval
            reconcile(self.runner.project, actor="apm", retarget=True)
        # Eager mode: which task PRs changed since their split. Recorded for the UI either way;
        # `scan` starts the redos only with `automation.eager_redo` on.
        with contextlib.suppress(Exception):
            plans = detect_redos(self.runner.project, self.runner.store)
            self.redos = {plan.feature: plan for plan in plans}
        return True

    def scan(self) -> None:
        """Queues the task and feature reviews that are due and drops queued ones that no longer
        are.
        """
        project = self.runner.project
        due: dict[tuple[str, NodeId], str] = {}
        automation = read_config(project.apm_dir).automation
        graph, body = load_graph(project), partial(read_body, project.apm_dir)
        if automation.task_review:
            self.forget_errored_task_reviews()
            for node_id in due_task_reviews(graph, self.runner.store, body):
                due[TASK_REVIEWER, node_id] = content_fingerprint(graph[node_id], body(node_id))
        if automation.feature_review:
            for step in due_feature_reviews(graph):
                due[FEATURE_REVIEWER, step] = ",".join(sorted(graph.dependencies(step)))
        if automation.implement_follow_ups:
            for node_id in passing_follow_ups(graph, self.runner.store, body):
                due[IMPLEMENTER, node_id] = content_fingerprint(graph[node_id], body(node_id))
        if automation.eager_redo:
            for feature, plan in self.redos.items():
                due[EAGER_REDO, feature] = plan.signature  # one automatic redo per change
        for key in [k for k in self.queued if k not in due]:
            del self.queued[key]
        for (agent, node_id), version in due.items():
            self.push(agent, node_id, version)

    def forget_errored_task_reviews(self) -> None:
        """Lets the store's error count decide whether a task review that errored runs again."""
        for node_id, run_id in list(self.task_runs.items()):
            latest = self.runner.store.latest_task_review(node_id)
            if latest is not None and latest.run_id == run_id and latest.verdict == "error":
                self.started.pop((TASK_REVIEWER, node_id), None)
                del self.task_runs[node_id]

    def pump(self) -> None:
        """Starts the runs whose wait is over, oldest first, while there is room."""
        ripe = sorted((at, key, v) for key, (v, at) in self.queued.items() if at <= self.clock())
        limit = read_config(self.runner.project.apm_dir).runner.max_concurrent if ripe else 0
        for _, key, version in ripe:
            if self.runner.active() >= limit:
                return
            try:
                run = self._start(*key)
                self.started[key] = version
                if key[0] == TASK_REVIEWER and run is not None:  # None: a test's stand-in
                    self.task_runs[key[1]] = run.id
            except RunnerBusy:  # its node is busy: try again later
                continue
            except ApmError, KeyError:  # it no longer applies (e.g. the node is gone): drop it
                pass
            del self.queued[key]

    def _start(self, agent: str, node_id: NodeId) -> Run:
        if agent == EAGER_REDO:
            return self.runner.eager.start_redo(node_id, self.redos.get(node_id))
        return self.runner.start(agent, node_id, critic=False)

    async def drive(self, events: asyncio.Queue[dict[str, Any]], tick_s: float = 0.5) -> None:
        """Runs for the life of the server: scans at startup and on every plan change."""
        rescan = True
        while True:
            with contextlib.suppress(ApmError):  # a broken apm.yaml waits for its fix
                if await asyncio.to_thread(self.reconcile):
                    rescan = True
                if rescan:
                    await asyncio.to_thread(self.scan)
                self.manual.pump()
                self.pump()
            rescan = False
            with contextlib.suppress(TimeoutError):
                rescan = (await asyncio.wait_for(events.get(), tick_s))["type"] in RESCAN_ON


def due_feature_reviews(graph: Graph) -> list[NodeId]:
    """The review steps that are ready. Their version is the tasks they wait on, so a step that
    waits on new follow-ups is reviewed again once those are done.
    """
    return [n for n in graph.nodes if graph[n].kind is Kind.REVIEW and graph.states[n].ready]


def passing_follow_ups(graph: Graph, store: Store, body: Callable[[NodeId], str]) -> list[NodeId]:
    """The ready follow-ups of feature reviews whose current content passed its task review.
    Tasks created any other way are left to you.
    """
    due = []
    for node_id in sorted(store.follow_ups() & set(graph.nodes)):
        latest = store.latest_task_review(node_id)
        if not graph.states[node_id].ready or latest is None or latest.verdict != "pass":
            continue
        if latest.fingerprint == content_fingerprint(graph[node_id], body(node_id)):
            due.append(node_id)
    return due
