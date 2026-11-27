"""The feature report's record and prompt (plans.md §7.10). APM gathers everything it knows about
a feature into the reporter's first message (descriptions, decisions, what people typed and chose,
proposals and their edits, the plan's history, review turns, what the branch changes), so the
agent never digs for decisions or invents them. The record stays within `RECORD_LIMIT`: Claude's
replies are cut first; a message a person typed is only ever shortened.
"""

import contextlib
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apm.core.config import ReportDetail
from apm.core.errors import ApmError, format_issue
from apm.core.feature_review import FeatureReview, ReviewRefused, parse_review
from apm.core.files import read_body
from apm.core.git import git, rev_parse
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import Kind, NodeId, Status
from apm.core.project import Project, load_graph
from apm.core.store import PROJECT, Proposal, Run, RunEvent, Store

REPORTER = "apm-feature-reporter"
RECORD_LIMIT = 60_000
"""About 15,000 tokens: room for a busy feature, with plenty left for reading code."""
BODY_LIMIT = 6_000
"""The feature's own description."""
NODE_LIMIT = 2_000
"""Each task's or decision's description."""
LEVELS = ((800, 2_000), (300, 2_000), (0, 2_000), (0, 600))
"""(characters of each Claude reply, characters of each typed message), tried in turn until the
record fits. A reply of 0 characters is left out; a typed message is always kept.
"""
HISTORY_LIMIT = 200
"""Plan changes listed, the most recent ones."""
LIST_LIMIT = 120
"""Lines of the diff stat, and of the branch's commits."""

LABELS = {
    "apm-assistant": "Chat",
    "apm-decomposer": "Decompose",
    "apm-implementer": "Implement",
    "apm-feature-reviewer": "Review feature",
    "apm-task-reviewer": "Review task",
}
"""What a conversation was, by the agent that started it (`apm.runner.agents` has the same)."""
QUIET = ("apm-critic", REPORTER)
"""Agents whose turns say nothing a person typed: the critic is APM's, and a report's guidance
is about the report, not the feature.
"""

# How `apm.runner.agents.build_prompt` and the runner word what they write around your words.
# tests/core/test_report.py checks these against the real prompts.
GUIDANCE = "\n\nGuidance from the user:\n"
LOOKING_AT = "The user is looking at "
REVISION = "A reviewer looked at your breakdown"
DEFAULT_CHATS = (
    "Give me a short overview of the plan: where it stands and what's next.",
    "Summarise this node: what it is for, its state, and what's next.",
)
NO_ANSWER = "Nobody answered at the terminal."
SKIPPED = "The user skipped the question."

NonEmpty = Annotated[str, Field(min_length=1)]


class ReportRefused(ApmError, ValueError):  # noqa: N818 (a refusal, not a failure)
    """No report can be written for this node now, for a reason the user can fix."""


# --- what a person typed ---


def typed_text(run: Run) -> str | None:
    """What a person typed to start or continue `run`, without what APM wrote around it. None if
    APM wrote all of it: an automatic run, or one started without guidance.
    """
    prompt = run.prompt
    if run.agent in QUIET or prompt.startswith(REVISION):
        return None
    if run.agent == "apm-assistant":
        if prompt.startswith(LOOKING_AT) and "\n\n" in prompt:
            prompt = prompt.split("\n\n", 1)[1]
        text = prompt.strip()
        return None if text in DEFAULT_CHATS else text or None
    if GUIDANCE in prompt:
        return prompt.split(GUIDANCE, 1)[1].strip() or None
    if run.parent_run:  # the next turn of a conversation: the message itself
        return prompt.strip() or None
    return None


# --- the record ---


@dataclass(frozen=True, slots=True)
class Item:
    """A node inside the feature: a task, a decision, or a feature nested in it."""

    node_id: NodeId
    title: str
    kind: Kind
    status: Status
    depth: int
    """1 for a child of the feature, 2 for a grandchild, and so on."""
    body: str
    nested: bool
    """It has children: a feature inside the feature."""
    pr: str | None
    branch: str | None
    sizing: str | None
    """What its last task review said, e.g. `pass: About 30 lines.`"""


@dataclass(frozen=True, slots=True)
class Said:
    """One line of a conversation."""

    who: Literal["person", "claude"]
    text: str
    when: float
    how: Literal["said", "answered", "refused", "plan"] = "said"


@dataclass(frozen=True, slots=True)
class Conversation:
    title: str
    started: float
    lines: tuple[Said, ...]
    partial: bool = False
    """Only the turns that changed the feature, and the one before each: a chat elsewhere."""


@dataclass(frozen=True, slots=True)
class ProposalItem:
    where: str
    source: str
    status: str
    when: float
    rationale: str
    critic: str
    warnings: tuple[str, ...]
    children: tuple[str, ...]
    changes: tuple[str, ...]
    """What was changed before it was accepted."""
    became: tuple[str, ...]
    """The nodes it became."""


@dataclass(frozen=True, slots=True)
class PlanChange:
    when: int
    who: str
    subject: str
    note: str


@dataclass(frozen=True, slots=True)
class ReviewTurn:
    feature: str
    when: float
    branch: str
    sha: str
    review: FeatureReview
    follow_ups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BranchChanges:
    """What the feature branch changes against its home: where the agent should look."""

    branch: str
    home: str
    sha: str
    stat: str
    commits: str


@dataclass(frozen=True, slots=True)
class Record:
    feature: NodeId
    title: str
    status: Status
    body: str
    pr: str | None
    items: tuple[Item, ...]
    conversations: tuple[Conversation, ...]
    proposals: tuple[ProposalItem, ...]
    history: tuple[PlanChange, ...]
    bookkeeping: int
    """Plan commits on the feature that record no decision: APM's and the agents' own."""
    reviews: tuple[ReviewTurn, ...]
    changes: BranchChanges | None


def gather_record(
    project: Project, store: Store, feature: NodeId, changes: BranchChanges | None = None
) -> Record:
    """Everything APM knows about how `feature` came to be. `changes` is what its branch
    changes, worked out by the caller (it needs the code repo).
    """
    graph = load_graph(project)
    subtree = [feature, *graph.descendants(feature)]
    # A task split in place is gone from the graph, but its conversations and its split belong to
    # the feature its parts are in.
    subtree += _split_away(store, graph, set(subtree))
    history, bookkeeping, runs = _history(project, set(subtree), _accepted_by_apm(store, subtree))
    step = graph.review_step(feature)
    delivery = graph[step].delivery if step is not None else None
    return Record(
        feature=feature,
        title=graph[feature].title,
        status=graph[feature].status,
        body=read_body(project.apm_dir, feature),
        pr=delivery.pr if delivery is not None else None,
        items=tuple(_items(project, store, graph, feature)),
        conversations=tuple(_conversations(store, graph, subtree, runs)),
        proposals=tuple(_proposals(store, graph, subtree)),
        history=tuple(history),
        bookkeeping=bookkeeping,
        reviews=tuple(_reviews(store, graph, subtree)),
        changes=changes,
    )


def _items(project: Project, store: Store, graph: Graph, feature: NodeId) -> Iterator[Item]:
    top = graph.depth(feature)
    for node_id in graph.descendants(feature):
        node = graph[node_id]
        if node.kind is Kind.REVIEW:
            continue  # its verdicts are under the feature reviews
        leaf = graph.is_leaf(node_id)
        sizing = None
        review = store.latest_task_review(node_id) if leaf and node.kind is Kind.WORK else None
        if review is not None and review.verdict != "error":
            sizing = f"{review.verdict}: {review.data.get('summary', '')}".strip(": ")
        delivery = node.delivery
        yield Item(
            node_id=node_id,
            title=node.title,
            kind=node.kind,
            status=node.status,
            depth=graph.depth(node_id) - top,
            body=read_body(project.apm_dir, node_id),
            nested=not leaf,
            pr=delivery.pr if delivery is not None else None,
            branch=delivery.branch if delivery is not None else None,
            sizing=sizing,
        )


# --- the plan's history ---


@dataclass(frozen=True, slots=True)
class _Commit:
    time: int
    subject: str
    note: str
    trailers: dict[str, str]


_TRAILER = re.compile(r"^APM-[A-Za-z-]+: ")


def _plan_log(project: Project) -> Iterator[_Commit]:
    """Every plan commit, oldest first, with its note (the commit body) and its trailers."""
    if rev_parse(project.apm_dir, "HEAD") is None:
        return
    out = git(project.apm_dir, ["log", "--reverse", "--format=%ct%x00%s%x00%b%x1e"])
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        time, subject, body = record.split("\0", 2)
        trailers: dict[str, str] = {}
        note = []
        for line in body.splitlines():
            if _TRAILER.match(line):
                key, _, value = line.partition(":")
                trailers.setdefault(key, value.strip())
            else:
                note.append(line)
        yield _Commit(int(time), subject, "\n".join(note).strip(), trailers)


def _history(
    project: Project, nodes: set[NodeId], by_apm: set[str]
) -> tuple[list[PlanChange], int, set[str]]:
    """The plan commits that touched the feature's nodes, oldest first: those that record a
    decision (a change a person made, a breakdown accepted, a setting, a change made in a chat),
    how many others there were, and the runs any of them came from. `by_apm` are the runs whose
    proposals APM accepted on its own: those breakdowns are nobody's decision.
    """
    shown: list[PlanChange] = []
    bookkeeping = 0
    runs: set[str] = set()
    for commit in _plan_log(project):
        if not nodes & set(commit.trailers.get("APM-Nodes", "").split(",")):
            continue
        run = commit.trailers.get("APM-Run")
        if run:
            runs.add(run)
        actor = commit.trailers.get("APM-Actor", "user")  # settings commits name no actor
        op = commit.trailers.get("APM-Op", "")
        accepted = op in ("decompose", "split", "settings")
        if actor == "user" or accepted or actor == "agent:apm-assistant":
            who = _who(actor, op, automatic=run in by_apm)
            shown.append(PlanChange(commit.time, who, commit.subject, commit.note))
        else:
            bookkeeping += 1
    return shown, bookkeeping, runs


def _who(actor: str, op: str, *, automatic: bool) -> str:
    if actor == "user":
        return "the user"
    agent = actor.removeprefix("agent:")
    if op in ("decompose", "split"):
        who = "APM on its own" if automatic else "the user"
        return f"{who}, accepting {agent}'s proposal"
    if agent == "apm-assistant":
        return "Claude in a chat, as asked there"
    return agent


def _split_away(store: Store, graph: Graph, nodes: set[NodeId]) -> list[NodeId]:
    """The tasks that a split replaced with parts among `nodes`."""
    gone = []
    for proposal in store.list_proposals(status="accepted"):
        acceptance = store.acceptance(proposal.id)
        if proposal.node_id not in graph and acceptance and nodes & set(acceptance.created):
            gone.append(proposal.node_id)
    return gone


def _accepted_by_apm(store: Store, subtree: list[NodeId]) -> set[str]:
    """The runs whose proposals APM accepted on its own: a feature review's follow-ups, with
    `automation.accept_follow_ups` on (`apm.core.feature_review.record_review`).
    """
    follow_ups = store.follow_ups()
    runs = set()
    for node_id in subtree:
        for proposal in store.list_proposals(node_id=node_id, status="accepted"):
            acceptance = store.acceptance(proposal.id)
            if proposal.run_id and acceptance and follow_ups & set(acceptance.created):
                runs.add(proposal.run_id)
    return runs


# --- what people said ---


def _conversations(
    store: Store, graph: Graph, subtree: list[NodeId], changed_by: set[str]
) -> list[Conversation]:
    """The conversations about the feature's nodes, and, from conversations elsewhere (a chat
    about the whole plan, say), the turns that changed the feature's part of the plan and the
    turn before each. Only those where a person said or chose something.
    """
    runs = {r.id: r for node_id in subtree for r in store.list_runs(node_id=node_id, limit=100_000)}
    out = [_conversation(store, graph, thread) for thread in _threads(runs.values()).values()]
    elsewhere: dict[NodeId, set[str]] = defaultdict(set)
    for run_id in changed_by - runs.keys():
        with contextlib.suppress(KeyError):  # a run from before the database was cleared
            elsewhere[store.get_run(run_id).node_id].add(run_id)
    for node_id, wanted in elsewhere.items():
        for thread in _threads(store.list_runs(node_id=node_id, limit=100_000)).values():
            keep = {i for i, run in enumerate(thread) if run.id in wanted}
            keep |= {i - 1 for i in keep if i > 0}
            if keep:
                turns = [run for i, run in enumerate(thread) if i in keep]
                out.append(_conversation(store, graph, turns, partial=len(turns) < len(thread)))
    said = [c for c in out if any(line.who == "person" for line in c.lines)]
    return sorted(said, key=lambda c: c.started)


def _threads(runs: Iterable[Run]) -> dict[str, list[Run]]:
    """Runs grouped into conversations (each run follows its `parent_run`), oldest turn first."""
    by_id = {run.id: run for run in runs}

    def root(run: Run) -> str:
        seen = {run.id}
        while run.parent_run in by_id and run.parent_run not in seen:
            run = by_id[run.parent_run]
            seen.add(run.id)
        return run.id

    threads: dict[str, list[Run]] = defaultdict(list)
    for run in by_id.values():
        threads[root(run)].append(run)
    return {key: sorted(turns, key=lambda r: r.started) for key, turns in threads.items()}


def _conversation(
    store: Store, graph: Graph, turns: list[Run], *, partial: bool = False
) -> Conversation:
    first = turns[0]
    what = LABELS.get(first.agent, first.agent)
    where = (
        "about the whole plan" if first.node_id == PROJECT else f"on {_ref(graph, first.node_id)}"
    )
    lines: list[Said] = []
    for run in turns:
        if run.agent in QUIET:
            continue
        if text := typed_text(run):
            lines.append(Said("person", text, run.started))
        lines += _turn(store.list_events(run.id), run.ended or run.started)
    return Conversation(f"{what} {where}", first.started, tuple(lines), partial)


def _turn(events: list[RunEvent], ended: float) -> list[Said]:
    """What Claude said in one turn, and what a person chose when it asked."""
    lines: list[Said] = []
    reply: list[str] = []
    asked: dict[str, dict[str, Any]] = {}

    def flush(when: float) -> None:
        if text := "\n\n".join(reply).strip():
            lines.append(Said("claude", text, when))
        reply.clear()

    for event in events:
        data = event.data
        kind = data.get("type")
        if kind == "assistant":
            content = (data.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text":
                    reply.append(str(block.get("text", "")).strip())
        elif kind == "apm_request":
            asked[str(data.get("id"))] = data
        elif kind == "apm_request_resolved" and (request := asked.get(str(data.get("id")))):
            flush(event.ts)
            lines += _choice(request, data, event.ts)
    flush(ended)
    return lines


def _choice(request: dict[str, Any], answer: dict[str, Any], when: float) -> list[Said]:
    """What a person chose when Claude asked: answers, a plan approved, a refusal and its reason.
    Allowing a command records no decision, and neither does a prompt nobody answered.
    """
    message = str(answer.get("message") or "").strip()
    allowed = bool(answer.get("allow"))
    if message == NO_ANSWER:
        return []
    if request.get("kind") == "question":
        if not allowed or message == SKIPPED:
            questions = [
                q.get("question", "") for q in request.get("input", {}).get("questions", [])
            ]
            asked = "; ".join(f"“{q}”" for q in questions if q)
            return [Said("person", f"Skipped Claude's question {asked}".strip(), when, "answered")]
        answers = answer.get("answers") or {}
        return [Said("person", f"“{q}”: {a}", when, "answered") for q, a in answers.items()]
    if request.get("kind") == "plan":
        text = "Approved Claude's plan." if allowed else "Asked Claude to keep planning."
        return [Said("person", f"{text} {message}".strip(), when, "plan")]
    if allowed:
        return []
    tool_input = request.get("input") or {}
    what = tool_input.get("command") or request.get("description") or request.get("tool", "")
    text = f"Refused {request.get('tool', 'a tool')}: `{_one_line(str(what), 200)}`"
    return [Said("person", f"{text}. Reason: {message}" if message else text, when, "refused")]


# --- proposals ---


SOURCES = {
    "apm-decomposer": "Breakdown by Decompose",
    "apm-task-reviewer": "Split proposed by a task review (the task was too big; accepted, the"
    " parts replace it in its feature)",
    "apm-feature-reviewer": "Follow-up tasks proposed by a feature review",
}


def _proposals(store: Store, graph: Graph, subtree: list[NodeId]) -> list[ProposalItem]:
    follow_ups = store.follow_ups()
    found = [p for node_id in subtree for p in store.list_proposals(node_id=node_id)]
    return [_proposal(store, graph, p, follow_ups) for p in sorted(found, key=lambda p: p.created)]


def _proposal(
    store: Store, graph: Graph, proposal: Proposal, follow_ups: set[NodeId]
) -> ProposalItem:
    run = _run(store, proposal.run_id)
    agent = run.agent if run is not None else ""
    data = proposal.data
    proposed = [c for c in data.get("children", []) if isinstance(c, dict)]
    acceptance = store.acceptance(proposal.id) if proposal.status == "accepted" else None
    changes: list[str] = []
    became: list[str] = []
    status: str = proposal.status
    if acceptance is not None:
        changes = proposal_changes(proposed, acceptance.children)
        became = [_state(graph, n) for n in acceptance.created]
        if follow_ups & set(acceptance.created):
            status = "accepted by APM on its own (automation.accept_follow_ups)"
        elif changes:
            status = "accepted by the user, after changing it"
        else:
            status = "accepted by the user as proposed"
    elif proposal.status == "rejected" and run is not None and _replaced(store, run, proposal):
        status = "replaced by the next turn of the same feature review"
    elif proposal.status == "rejected":
        status = "rejected by the user"
    elif proposal.status == "pending":
        status = "waiting for a decision"
    review = proposal.review or {}
    critic = ""
    if review:
        revised = " (the breakdown was revised once in response)" if review.get("revised") else ""
        critic = f"{review.get('verdict', '')}{revised}: {review.get('summary', '')}".strip(": ")
    split_of = data.get("split_of")
    where = _ref(graph, proposal.node_id)
    if proposal.node_id not in graph and isinstance(split_of, dict) and split_of.get("title"):
        where = f"{handle(proposal.node_id)} “{split_of['title']}” (replaced by its split)"
    return ProposalItem(
        where=where,
        source=SOURCES.get(agent, "Proposal"),
        status=status,
        when=proposal.created,
        rationale=str(data.get("rationale") or ""),
        critic=critic,
        warnings=tuple(str(w) for w in data.get("warnings") or []),
        children=tuple(_child(c) for c in proposed),
        changes=tuple(changes),
        became=tuple(became),
    )


def _replaced(store: Store, run: Run, proposal: Proposal) -> bool:
    """True if a later turn of the same feature review rejected this proposal when it replaced
    it (`apm.core.feature_review.record_review`), rather than a person.
    """
    if run.agent != "apm-feature-reviewer" or proposal.resolved is None:
        return False
    later = store.list_runs(node_id=run.node_id, limit=1000)
    return any(
        r.agent == run.agent
        and r.started > run.started
        and r.ended is not None
        # The rejection comes right after that turn ends, as its verdict is recorded.
        and 0 <= proposal.resolved - r.ended < 120
        for r in later
    )


def _child(child: dict[str, Any]) -> str:
    text = f"[{child.get('key', '?')}] {child.get('title', '')} ({child.get('kind', 'work')})"
    if intent := str(child.get("intent") or "").strip():
        text += f": {_one_line(intent, 300)}"
    return text


def proposal_changes(
    proposed: list[dict[str, Any]], accepted: list[dict[str, Any]] | None
) -> list[str]:
    """What was changed in a proposal before it was accepted, one line per change. Children are
    matched by their key. `accepted` None means it was accepted whole.
    """
    if accepted is None:
        return []
    after = {str(c.get("key")): c for c in accepted}
    lines = []
    for child in proposed:
        key = str(child.get("key"))
        name = f'[{key}] "{child.get("title", "")}"'
        if key not in after:
            lines.append(f"Left out {name}.")
            continue
        new = after[key]
        if _text(new.get("title")) != _text(child.get("title")):
            lines.append(f'Renamed {name} to "{_text(new.get("title"))}".')
        if (new.get("kind") or "work") != (child.get("kind") or "work"):
            lines.append(
                f"Made {name} a {new.get('kind')} instead of a {child.get('kind') or 'work'}."
            )
        if _text(new.get("intent")) != _text(child.get("intent")):
            lines.append(
                f"Changed the intent of {name} from “{_one_line(_text(child.get('intent')), 300)}”"
                f" to “{_one_line(_text(new.get('intent')), 300)}”."
            )
        lines += _list_change(
            name, "acceptance criterion", child.get("acceptance"), new.get("acceptance")
        )
        lines += _list_change(
            name, "dependency on", child.get("depends_on_keys"), new.get("depends_on_keys")
        )
        lines += _list_change(
            name, "dependency on", child.get("depends_on_ids"), new.get("depends_on_ids")
        )
    known = {str(c.get("key")) for c in proposed}
    for key, child in after.items():
        if key not in known:
            lines.append(f'Added [{key}] "{child.get("title", "")}", which wasn\'t proposed.')
    return lines


def _list_change(name: str, what: str, before: object, after: object) -> list[str]:
    old = [_text(v) for v in before or [] if _text(v)] if isinstance(before, list) else []
    new = [_text(v) for v in after or [] if _text(v)] if isinstance(after, list) else []
    lines = [f"Added to {name} the {what} “{v}”." for v in new if v not in old]
    lines += [f"Removed from {name} the {what} “{v}”." for v in old if v not in new]
    return lines


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


# --- feature reviews ---


def _reviews(store: Store, graph: Graph, subtree: list[NodeId]) -> Iterator[ReviewTurn]:
    """Every turn of every review of the feature and the features inside it, oldest first."""
    for step in subtree:
        if step not in graph:  # a task split away
            continue
        if graph[step].kind is not Kind.REVIEW or (feature := graph[step].parent) is None:
            continue
        runs = [
            r
            for r in store.list_runs(node_id=step, limit=1000)
            if r.agent == "apm-feature-reviewer"
        ]
        for run in sorted(runs, key=lambda r: r.started):
            events = store.list_events(run.id)
            answer = next(
                (e.data.get("structured_output") for e in events if e.data.get("type") == "result"),
                None,
            )
            if run.status != "succeeded" or answer is None:
                continue
            try:
                review = parse_review(answer)
            except ReviewRefused:
                continue
            target = _review_target(store, run) or {}
            yield ReviewTurn(
                feature=_ref(graph, feature),
                when=run.started,
                branch=str(target.get("branch", "?")),
                sha=str(target.get("sha", "?")),
                review=review,
                follow_ups=tuple(_follow_ups(store, graph, feature, run)),
            )


def _review_target(store: Store, run: Run) -> dict[str, Any] | None:
    """The commit a review conversation is about, as its first turn recorded it."""
    current: Run | None = run
    while current is not None:
        for event in store.list_events(current.id):
            if event.data.get("type") == "apm_review_target":
                return event.data
        current = _run(store, current.parent_run)
    return None


def _follow_ups(store: Store, graph: Graph, feature: NodeId, run: Run) -> Iterator[str]:
    for proposal in store.list_proposals(node_id=feature):
        if proposal.run_id != run.id:
            continue
        acceptance = store.acceptance(proposal.id)
        if acceptance is None:
            count = len(proposal.data.get("children", []))
            yield f"{count} proposed, {proposal.status}"
            continue
        yield from (_state(graph, node_id) for node_id in acceptance.created)


# --- small helpers ---


def _run(store: Store, run_id: str | None) -> Run | None:
    if not run_id:
        return None
    try:
        return store.get_run(run_id)
    except KeyError:
        return None


def _ref(graph: Graph, node_id: NodeId) -> str:
    if node_id in graph:
        return f"{graph.handles[node_id]} “{graph[node_id].title}”"
    return f"{handle(node_id)} (deleted since)"


def _state(graph: Graph, node_id: NodeId) -> str:
    """A node, with its status and pull request: `9a8b7c6d “Handle errors” (done, PR …)`."""
    if node_id not in graph:
        return _ref(graph, node_id)
    node = graph[node_id]
    pr = f", PR {node.delivery.pr}" if node.delivery is not None and node.delivery.pr else ""
    return f"{_ref(graph, node_id)} ({node.status}{pr})"


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int) -> str:
    """`text`, cut to about `limit` characters, saying how much was cut."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()} … [{len(text) - limit} more characters cut]"


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _quote(text: str) -> str:
    """`text` as a markdown block quote, so headings in it don't break the record's outline."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.strip().splitlines())


# --- rendering ---


def render_record(record: Record, *, limit: int = RECORD_LIMIT) -> str:
    """The record as markdown, within `limit` characters if it can be: Claude's replies are
    shortened and then left out, and typed messages shortened, until it fits. A message a person
    typed is never left out.
    """
    text = ""
    for replies, typed in LEVELS:
        text = _render(record, replies=replies, typed=typed)
        if len(text) <= limit:
            break
    return text


def _render(record: Record, *, replies: int, typed: int) -> str:
    parts = [
        f"# The record of feature {handle(record.feature)} “{record.title}”",
        "APM gathered this from the plan, its history, the conversations and the code repo."
        " Times are UTC. “The user” is the person working with APM.",
        *_feature_part(record),
        *_items_part(record),
        *_said_part(record, replies=replies, typed=typed),
        *_proposals_part(record),
        *_history_part(record),
        *_reviews_part(record),
        *_changes_part(record),
    ]
    return "\n\n".join(p for p in parts if p) + "\n"


def _feature_part(record: Record) -> list[str]:
    facts = [f"- Node: {handle(record.feature)} ({record.feature}), {record.status}"]
    if (changes := record.changes) is not None:
        facts.append(f"- Feature branch: `{changes.branch}`, going into `{changes.home}`")
    if record.pr:
        facts.append(f"- Pull request: {record.pr}")
    return [
        "## The feature",
        "\n".join(facts),
        "### Its description",
        _body(record.body, BODY_LIMIT),
    ]


def _items_part(record: Record) -> list[str]:
    decisions = [i for i in record.items if i.kind is Kind.DECISION]
    parts: list[str] = []
    if decisions:
        parts.append("## Decisions recorded in the plan")
        for item in decisions:
            parts += [
                f"### {handle(item.node_id)} “{item.title}” ({item.status})",
                _body(item.body),
            ]
    work = [i for i in record.items if i.kind is not Kind.DECISION]
    parts.append("## Tasks")
    if not work:
        parts.append("(none)")
    for item in work:
        level = "#" * min(2 + item.depth, 6)
        if item.nested:
            parts += [
                f"{level} Feature {handle(item.node_id)} “{item.title}” ({item.status})",
                _body(item.body),
            ]
            continue
        facts = [f"Status: {item.status}"]
        if item.pr:
            facts.append(f"pull request {item.pr}")
        if item.branch:
            facts.append(f"branch `{item.branch}`")
        if item.sizing:
            facts.append(f"task review: {item.sizing}")
        heading = f"{level} {handle(item.node_id)} “{item.title}”"
        parts += [heading, "; ".join(facts).rstrip(".") + ".", _body(item.body)]
    return parts


def _body(text: str, limit: int = NODE_LIMIT) -> str:
    """A node's description, quoted, without the empty sections of the template."""
    text = _without_empty_sections(text)
    return _quote(_clip(text, limit)) if text else "(no description)"


def _without_empty_sections(text: str) -> str:
    """`## Notes` with nothing under it says nothing: drop such headings."""
    kept = []
    for part in re.split(r"(?m)^(?=#{1,6} )", text.strip()):
        part = part.strip()
        if part and not (part.startswith("#") and len(part.splitlines()) == 1):
            kept.append(part)
    return "\n\n".join(kept)


def _said_part(record: Record, *, replies: int, typed: int) -> list[str]:
    parts = ["## What people said"]
    if not record.conversations:
        return [*parts, "No conversation about this feature has anything a person typed or chose."]
    note = "Every message a person typed or choice they made in a conversation about this feature."
    if replies:
        note += f" Claude's replies are shortened to their last {replies} characters."
    else:
        note += " Claude's replies are left out, to keep this record short."
    parts.append(note)
    for conversation in record.conversations:
        title = f"### {conversation.title} · {_day(conversation.started)}"
        if conversation.partial:
            title += " (only the turns that changed this feature, and the turn before each)"
        lines = []
        for said in conversation.lines:
            if said.who == "claude":
                if replies:
                    lines.append(f"- Claude: {_tail(said.text, replies)}")
                continue
            who = {
                "said": "The user",
                "answered": "The user answered",
                "refused": "The user",
                "plan": "The user",
            }
            lines.append(
                f"- **{who[said.how]}** ({_day(said.when)}): {_indent(_clip(said.text, typed))}"
            )
        parts += [title, "\n".join(lines)]
    return parts


def _tail(text: str, limit: int) -> str:
    """The end of a reply, on as few lines as it takes: what the user answered is usually there."""
    text = " ".join(text.split())
    return text if len(text) <= limit else "… " + text[-limit:].lstrip()


def _indent(text: str) -> str:
    """Keeps a multi-line message inside its list item."""
    return text.replace("\n", "\n  ")


def _proposals_part(record: Record) -> list[str]:
    if not record.proposals:
        return []
    parts = ["## Proposals"]
    for item in record.proposals:
        parts.append(f"### {item.source} for {item.where} · {_day(item.when)} · {item.status}")
        lines = []
        if item.rationale:
            lines.append(f"Rationale: {_one_line(item.rationale, 1500)}")
        if item.critic:
            lines.append(f"Reviewer: {_one_line(item.critic, 800)}")
        lines += [f"Flagged: {_one_line(w, 400)}" for w in item.warnings]
        lines.append("Proposed:\n" + "\n".join(f"- {c}" for c in item.children))
        if item.changes:
            lines.append(
                "Changed by the user before accepting:\n"
                + "\n".join(f"- {c}" for c in item.changes)
            )
        if item.became:
            lines.append("Became: " + "; ".join(item.became))
        parts.append("\n\n".join(lines))
    return parts


def _history_part(record: Record) -> list[str]:
    if not record.history and not record.bookkeeping:
        return []
    parts = [
        "## The plan's history",
        "Changes to this feature's part of the plan that record a decision, oldest first, with"
        " the note that came with each.",
    ]
    shown = record.history[-HISTORY_LIMIT:]
    lines = []
    if len(record.history) > len(shown):
        lines.append(f"- ({len(record.history) - len(shown)} earlier changes left out)")
    for change in shown:
        line = f"- {_day(change.when)} · {change.who} · {change.subject}"
        if change.note:
            line += f": {_one_line(change.note, 600)}"
        lines.append(line)
    if record.bookkeeping:
        lines.append(
            f"- ({_count(record.bookkeeping, 'more commit was', 'more commits were')} APM's and"
            " the agents' own bookkeeping: statuses, deliveries, review notes)"
        )
    return [*parts, "\n".join(lines)]


_CHECKED = {
    "met": "Met",
    "partial": "Partly met",
    "missing": "Missing",
    "unverified": "Not checked",
}


def _reviews_part(record: Record) -> list[str]:
    if not record.reviews:
        return []
    parts = ["## Feature reviews", "Every turn of every review, oldest first."]
    for number, turn in enumerate(record.reviews, 1):
        review = turn.review
        verdict = "Complete" if review.complete else "Incomplete"
        parts.append(
            f"### Turn {number}: {turn.feature} · {_day(turn.when)} · `{turn.branch}` at"
            f" `{turn.sha[:7]}` · {verdict}"
        )
        lines = [review.summary.strip()] if review.summary.strip() else []
        if review.criteria:
            lines.append(
                "\n".join(
                    f"- {_CHECKED[c.status]}: {_one_line(c.criterion, 300)}"
                    + (f" ({_one_line(c.evidence, 300)})" if c.evidence else "")
                    for c in review.criteria
                )
            )
        if review.issues:
            lines.append(
                "Issues:\n"
                + "\n".join(
                    f"- {i.severity}: {_one_line(i.problem, 400)}"
                    + (f" ({i.where})" if i.where else "")
                    for i in review.issues
                )
            )
        if turn.follow_ups:
            lines.append("Its follow-up tasks: " + "; ".join(turn.follow_ups))
        parts.append("\n\n".join(lines))
    return parts


def _changes_part(record: Record) -> list[str]:
    changes = record.changes
    if changes is None:
        return []
    return [
        "## What the feature branch changes",
        f"`git diff --stat origin/{changes.home}...{changes.branch}` at `{changes.sha[:7]}`:",
        f"```\n{_lines(changes.stat)}\n```",
        "Its commits, newest first (`git log --first-parent`):",
        f"```\n{_lines(changes.commits)}\n```",
    ]


def _lines(text: str) -> str:
    lines = text.strip("\n").splitlines()
    if len(lines) <= LIST_LIMIT:
        return "\n".join(lines) or "(nothing)"
    return "\n".join([*lines[:LIST_LIMIT], f"… ({len(lines) - LIST_LIMIT} more lines)"])


# --- what the reporter is asked for, and what it returns ---


BRIEF = """About one page:

1. **What it is and why**: two or three sentences.
2. **Key decisions**: one line each, with the reason in a few words, or "no reason recorded".
3. **Pull requests**: one line each, the pull request and what it built."""

STANDARD = """1. **What it is and why it exists.**
2. **Decisions**: each decision a person made, with its rationale and the alternatives that were
   rejected.
3. **How it is put together**: the modules and how data flows between them.
4. **The work**: one paragraph per task, citing its pull request.
5. **How it was tested.**
6. **Left for later**: what was dropped, deferred or noted for later."""

DETAILED = (
    STANDARD
    + """
7. **Implementation notes**, file by file.
8. **Review findings**: what each feature review found and how it was fixed.
9. **Risks and follow-ups.**
10. **Timeline**: the main events, dated, oldest first."""
)

OUTLINES: dict[ReportDetail, str] = {"brief": BRIEF, "standard": STANDARD, "detailed": DETAILED}

RULES = """- Every human decision you report must come from the record below. Give only the reasons
  the record gives, in their words where you can, and never invent a rationale or present your
  own guess as theirs.
- Where a decision has no recorded reason, say so: "No reason was recorded."
- Cite the pull requests (#12) and files (`src/app/search.py`) behind what you say. Read the code
  in your working directory to describe how the feature works; don't guess.
- The record says what people wrote; the code says what was built. Where they disagree, say so.
- Write for people who weren't there: plain words, no internal shorthand, no copy of the record."""


def report_prompt(
    graph: Graph,
    feature: NodeId,
    *,
    branch: str,
    sha: str,
    home: str,
    path: str,
    detail: ReportDetail,
    record: str,
    message: str = "",
) -> str:
    """The reporter's first message: what to write at this detail level, the rules, the user's
    guidance, and the record.
    """
    lines = [
        f"Write the feature report for node {handle(feature)} (“{graph[feature].title}”) of the APM"
        f" plan, at **{detail}** detail. APM commits it as `{path}` on branch `{branch}`, so it"
        f" lands with the feature, for people who weren't there.",
        f"Your working directory is a checkout of `{branch}` at commit {sha}."
        f" `git diff origin/{home}...HEAD` shows everything the feature changes.",
        f"## What to write ({detail})",
        OUTLINES[detail],
        "## Rules",
        RULES,
    ]
    if message.strip():
        lines += ["## Guidance from the user", message.strip()]
    lines += [record, "Return the report: its markdown, and a one-line summary."]
    return "\n\n".join(lines)


REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "markdown"],
    "properties": {
        "summary": {"type": "string", "description": "One sentence: what the feature is and does."},
        "markdown": {
            "type": "string",
            "description": "The whole report in markdown, starting with a level-1 heading.",
        },
    },
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ReportAnswer(_Model):
    summary: str = ""
    markdown: NonEmpty


def parse_answer(data: object) -> ReportAnswer:
    try:
        answer = ReportAnswer.model_validate(data)
    except ValidationError as err:
        problems = "; ".join(format_issue(e) for e in err.errors())
        raise ReportRefused(f"not a valid report: {problems}") from None
    if not answer.markdown.strip():
        raise ReportRefused("not a valid report: the markdown is empty")
    return answer


def report_file(
    answer: ReportAnswer, feature: NodeId, detail: ReportDetail, when: datetime | None = None
) -> str:
    """The file APM commits: the agent's markdown, and a line saying where it came from."""
    day = (when or datetime.now(UTC)).date().isoformat()
    footer = (
        f"_Written by APM's feature reporter from the plan's record of feature `{handle(feature)}`,"
        f" at {detail} detail, on {day}._"
    )
    return f"{answer.markdown.strip()}\n\n---\n\n{footer}\n"
