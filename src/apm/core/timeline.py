"""A node's story for its Details: what you and Claude did to it, in a few words each, linked to
the conversation or pull request it came from. Built from the plan's history (its commits'
trailers), the task reviews, and the conversations on the node.
"""

import re
from dataclasses import dataclass

from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import NodeId
from apm.core.store import Run, Store
from apm.core.views import HistoryEntry

WHO = {
    "apm-assistant": "Claude, in a chat",
    "apm-implementer": "Claude (Implement)",
    "apm-decomposer": "Claude (Decompose)",
    "apm-critic": "Claude (Review)",
    "apm-task-reviewer": "Claude (Review task)",
    "apm-feature-reviewer": "Claude (Review feature)",
    "apm-feature-reporter": "Claude (Write report)",
    "apm-eager-builder": "Claude (Eager build)",
    "apm-splitter": "Claude (Split)",
}
VERDICT = {
    "pass": "Task review passed",
    "too_big": "Task review: too big, a split proposed",
    "unclear": "Task review: too unclear to size",
    "error": "Task review failed",
}
STATUS = {"todo": "To do", "in_progress": "In progress", "done": "Done", "dropped": "Dropped"}
CONVERSATION = {
    "apm-assistant": "A chat about it",
    "apm-implementer": "Started building it",
    "apm-decomposer": "Drafted a breakdown",
    "apm-feature-reviewer": "Reviewed the feature",
    "apm-feature-reporter": "Wrote the feature report",
    "apm-eager-builder": "Built the feature in one run",
}
_URL = re.compile(r"https?://[^\s,]+")


@dataclass(frozen=True, slots=True)
class Moment:
    time: float
    """Unix seconds."""
    text: str
    who: str
    run: str | None = None
    """The conversation it happened in, to open."""
    url: str | None = None
    """A pull request it is about."""


def who(actor: str) -> str:
    if actor in ("", "user"):
        return "You"
    if actor == "apm":
        return "APM"
    agent = actor.removeprefix("agent:")
    return WHO.get(agent, agent)


def node_timeline(
    store: Store, graph: Graph, node_id: NodeId, history: list[HistoryEntry]
) -> list[Moment]:
    """What happened to `node_id`, oldest first. Repeats in a row (five edits of the description)
    become one moment that says how many.
    """
    # `history` is newest first; commits keep whole seconds, so ties keep their order this way.
    moments = [m for entry in reversed(history) if (m := _change(entry, node_id)) is not None]
    moments += [
        Moment(r.created, VERDICT.get(r.verdict, r.verdict), WHO["apm-task-reviewer"], r.run_id)
        for r in store.task_reviews(node_id)
    ]
    moments += [
        Moment(run.started, CONVERSATION[run.agent], WHO.get(run.agent, run.agent), run.id)
        for run in store.list_runs(node_id=node_id, limit=200)
        if run.parent_run is None and run.agent in CONVERSATION
    ]
    return _collapse(sorted(moments, key=lambda m: m.time))


def _change(entry: HistoryEntry, node_id: NodeId) -> Moment | None:
    """A plan commit, from where `node_id` stands. Its subject is `<op> <head>: <detail>`."""
    head, _, detail = entry.subject.removeprefix(entry.op).strip().partition(": ")
    mine = head.strip() == handle(node_id)
    url = None
    match entry.op:
        case "create":
            text = "Created"
        case "decompose":
            text = f"Broken down into {detail}" if mine else "Created, in a breakdown"
        case "split":
            text = f"Created by splitting “{detail.split(' → ')[0]}”"
        case "status":
            text = f"Set to {STATUS.get(detail.strip(), detail)}"
        case "deliver":
            url = next(iter(_URL.findall(detail)), None)
            text = "Pull request recorded" if url else "Branch pushed"
        case "edit-body":
            text = "Description edited"
        case "update":
            text = f"Changed its {detail}" if detail else "Changed"
        case "tags":
            text = f"Tags: {detail}"
        case "link" | "unlink":
            src, _, dst = head.partition(" → ") if entry.op == "link" else head.partition(" from ")
            needs = "Now needs" if entry.op == "link" else "No longer needs"
            text = (
                f"{needs} {dst.strip()}"
                if src.strip() == handle(node_id)
                else "A dependency changed"
            )
        case "move":
            text = "Moved"
        case "merge":
            text = "Merged with other nodes"
        case "delete" | "settings":
            return None
        case _:
            text = entry.subject
    return Moment(entry.time, text, who(entry.actor), entry.run, url)


def _collapse(moments: list[Moment]) -> list[Moment]:
    out: list[Moment] = []
    texts: list[str] = []  # each moment's text before "(n times)"
    count = 1
    for moment in moments:
        if out and texts[-1] == moment.text and out[-1].who == moment.who:
            count += 1
            out[-1] = Moment(moment.time, f"{moment.text} ({count} times)", moment.who, moment.run)
            continue
        count = 1
        out.append(moment)
        texts.append(moment.text)
    return out


def builder(store: Store, node_id: NodeId, history: list[HistoryEntry]) -> Run | None:
    """The conversation building `node_id`: its latest Implement run, or else the conversation that
    set it in progress (a chat about the whole plan, say). None if you set it by hand.
    """
    implements = [
        r for r in store.list_runs(node_id=node_id, limit=50) if r.agent == "apm-implementer"
    ]
    if implements:
        return implements[0]
    for entry in history:  # newest first
        if entry.op == "status" and entry.run and entry.subject.endswith("in_progress"):
            try:
                return store.get_run(entry.run)
            except KeyError:
                return None
    return None
