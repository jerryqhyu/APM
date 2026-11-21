"""The runs `apm serve` starts on its own.

Automations push `(agent, node, version)` onto one `AutoQueue`. A push whose version (what the
run would work on, e.g. a task's content fingerprint) changed restarts that run's wait, so a burst
of edits ends in one run on the final content. `pump` starts the runs whose wait is over while
there is room under `runner.max_concurrent`; a run that can't start yet stays queued. One version
gets one automatic run per server: a run that errored is tried again after an edit or a restart.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from apm.core.config import read_config
from apm.core.errors import ApmError
from apm.core.files import read_body
from apm.core.model import NodeId
from apm.core.project import load_graph
from apm.core.store import content_fingerprint
from apm.core.task_review import due_task_reviews
from apm.runner.runner import Runner, RunnerBusy

TASK_REVIEWER = "apm-task-reviewer"
DEBOUNCE_S = 3.0
"""How long a run waits after the last change to what it works on."""


@dataclass
class AutoQueue:
    runner: Runner
    debounce_s: float = DEBOUNCE_S
    clock: Callable[[], float] = time.monotonic
    queued: dict[tuple[str, NodeId], tuple[str, float]] = field(default_factory=dict)
    """(agent, node) → (version, when it may start)."""
    started: dict[tuple[str, NodeId], str] = field(default_factory=dict)
    """(agent, node) → the version last started."""

    def push(self, agent: str, node_id: NodeId, version: str) -> None:
        key = (agent, node_id)
        if version not in (self.started.get(key), self.queued.get(key, ("",))[0]):
            self.queued[key] = (version, self.clock() + self.debounce_s)

    def scan(self) -> None:
        """Queues the task reviews that are due and drops queued ones that no longer are."""
        project, due = self.runner.project, {}
        if read_config(project.apm_dir).automation.task_review:
            graph, body = load_graph(project), partial(read_body, project.apm_dir)
            for node_id in due_task_reviews(graph, self.runner.store, body):
                due[node_id] = content_fingerprint(graph[node_id], body(node_id))
        for key in [k for k in self.queued if k[0] == TASK_REVIEWER and k[1] not in due]:
            del self.queued[key]
        for node_id, fingerprint in due.items():
            self.push(TASK_REVIEWER, node_id, fingerprint)

    def pump(self) -> None:
        """Starts the runs whose wait is over, oldest first, while there is room."""
        ripe = sorted((at, key, v) for key, (v, at) in self.queued.items() if at <= self.clock())
        limit = read_config(self.runner.project.apm_dir).runner.max_concurrent if ripe else 0
        for _, key, version in ripe:
            if self.runner.active() >= limit:
                return
            try:
                self.runner.start(*key, critic=False)
                self.started[key] = version
            except RunnerBusy:  # its node is busy: try again later
                continue
            except ApmError, KeyError:  # it no longer applies (e.g. the node is gone): drop it
                pass
            del self.queued[key]

    async def drive(self, events: asyncio.Queue[dict[str, Any]], tick_s: float = 0.5) -> None:
        """Runs for the life of the server: scans at startup and on every plan change."""
        rescan = True
        while True:
            with contextlib.suppress(ApmError):  # a broken apm.yaml waits for its fix
                if rescan:
                    await asyncio.to_thread(self.scan)
                self.pump()
            rescan = False
            with contextlib.suppress(TimeoutError):
                rescan = (await asyncio.wait_for(events.get(), tick_s))["type"] == "graph.changed"
