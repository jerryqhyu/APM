"""Finding conversations: the ones that worked on a node from elsewhere (a chat about the whole
plan that set a task in progress, say), and the Claude Code session behind each, so any
conversation can be opened, here or in a terminal.
"""

import shlex
from dataclasses import dataclass
from pathlib import Path

from apm.core.git import log
from apm.core.model import NodeId
from apm.core.project import Project
from apm.core.store import Run, Store


def runs_that_changed(project: Project, node_id: NodeId) -> set[str]:
    """The runs whose plan commits changed `node_id`, by the commits' `APM-Run` and `APM-Nodes`
    trailers.
    """
    return {
        run
        for entry in log(project.apm_dir)
        if (run := entry.trailers.get("APM-Run"))
        and node_id in entry.trailers.get("APM-Nodes", "").split(",")
    }


def conversation_of(store: Store, run_id: str) -> list[Run]:
    """Every turn of the conversation `run_id` is in, oldest first: the runs about the same node
    or plan that follow one another through `parent_run`.
    """
    run = store.get_run(run_id)
    peers = {r.id: r for r in store.list_runs(node_id=run.node_id, limit=100_000)}

    def root(turn: Run) -> str:
        seen = {turn.id}
        while turn.parent_run in peers and turn.parent_run not in seen:
            turn = peers[turn.parent_run]
            seen.add(turn.id)
        return turn.id

    top = root(run)
    return sorted((r for r in peers.values() if root(r) == top), key=lambda r: r.started)


def related_runs(project: Project, store: Store, node_id: NodeId) -> list[Run]:
    """The conversations about other nodes, or the whole plan, that changed `node_id`: every turn
    of each, newest first. Runs on `node_id` itself are not among them.
    """
    found: dict[str, Run] = {}
    for run_id in runs_that_changed(project, node_id):
        if run_id in found:
            continue
        try:
            turns = conversation_of(store, run_id)
        except KeyError:  # a run from before the database was cleared
            continue
        found.update({r.id: r for r in turns if r.node_id != node_id})
    return sorted(found.values(), key=lambda r: r.started, reverse=True)


@dataclass(frozen=True, slots=True)
class Session:
    """The Claude Code session a run belongs to, and how to open it in a terminal."""

    session_id: str
    cwd: str | None
    """Where it ran: a session resumes only from the directory it started in."""

    @property
    def command(self) -> str:
        resume = f"claude --resume {shlex.quote(self.session_id)}"
        return f"cd {shlex.quote(self.cwd)} && {resume}" if self.cwd else resume


def session_of(store: Store, run_id: str) -> Session | None:
    """The latest session of `run_id`'s conversation, from its `system` event (Claude Code's
    first): None if it never started one.
    """
    for run in reversed(conversation_of(store, run_id)):
        if not run.session_id:
            continue
        cwd = None
        for event in store.list_events(run.id):
            if event.data.get("type") == "system" and isinstance(event.data.get("cwd"), str):
                cwd = event.data["cwd"]
                break
        if cwd is not None and not Path(cwd).is_dir():
            cwd = None  # a worktree that was removed since: resume from anywhere instead
        return Session(run.session_id, cwd)
    return None
