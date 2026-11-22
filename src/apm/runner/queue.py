"""Runs you asked for that wait their turn under `runner.max_concurrent`.

A feature-level action (Review all tasks, Implement all) can ask for more runs than may go at
once. They go through a `RunQueue`: what fits starts now, the rest waits, oldest first, and starts
as runs finish. `apm serve` pumps it from its automation loop (`AutoQueue.drive`), ahead of the
automatic runs; `apm review-tasks` and `apm implement` pump it themselves.

It is kept apart from `AutoQueue` on purpose. An entry here is a run you asked for, once: it isn't
debounced, isn't dropped because the automation wouldn't start it, and doesn't count as the
automatic run of a version (so a task whose content had its automatic review can still be
reviewed by hand, and the other way round).
"""

from collections.abc import Iterable
from dataclasses import dataclass

from apm.core.config import read_config
from apm.core.errors import ApmError
from apm.core.model import NodeId
from apm.core.store import Run
from apm.runner.runner import Runner


@dataclass(frozen=True, slots=True)
class Queued:
    """A run to start: `Runner.start`'s arguments."""

    agent: str
    node_id: NodeId
    message: str = ""
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class Dropped:
    """A queued run that couldn't start once its turn came, and why."""

    item: Queued
    reason: str


@dataclass(frozen=True, slots=True)
class Pumped:
    """What one `RunQueue.pump` did."""

    started: tuple[Run, ...] = ()
    dropped: tuple[Dropped, ...] = ()


class RunQueue:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.waiting: list[Queued] = []
        """Oldest first."""

    def add(self, items: Iterable[Queued]) -> None:
        """Queues runs. One already waiting for the same agent and node isn't queued twice."""
        for item in items:
            if not self.has(item.agent, item.node_id):
                self.waiting.append(item)

    def has(self, agent: str, node_id: NodeId) -> bool:
        return any(q.agent == agent and q.node_id == node_id for q in self.waiting)

    def pump(self) -> Pumped:
        """Starts waiting runs, oldest first, while there is room. A run that can't start when its
        turn comes (it is already going, its node changed or is gone) is dropped, with why.
        """
        if not self.waiting:
            return Pumped()
        limit = read_config(self.runner.project.apm_dir).runner.max_concurrent
        started: list[Run] = []
        dropped: list[Dropped] = []
        while self.waiting and self.runner.active() < limit:
            item = self.waiting.pop(0)
            try:
                started.append(
                    self.runner.start(item.agent, item.node_id, item.message, mode=item.mode)
                )
            except (ApmError, KeyError) as err:  # KeyError: the node is gone
                dropped.append(Dropped(item, _reason(err)))
        return Pumped(tuple(started), tuple(dropped))


def _reason(err: Exception) -> str:
    return str(err.args[0]) if isinstance(err, KeyError) and err.args else str(err)
