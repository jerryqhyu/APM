"""The feature report's record: what people said and chose, gathered from the plan, its history
and the store, and kept within a size; and what the reporter is asked for at each detail level.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from apm.core.files import read_body, render_body
from apm.core.ids import handle
from apm.core.model import Kind, NodeId, Status
from apm.core.ops import NewNode, create_nodes, edit_body, set_status
from apm.core.project import Project, load_graph
from apm.core.proposals import accept_proposal, reject_proposal
from apm.core.report import (
    OUTLINES,
    BranchChanges,
    ReportAnswer,
    ReportRefused,
    gather_record,
    parse_answer,
    proposal_changes,
    render_record,
    report_file,
    report_prompt,
    typed_text,
)
from apm.core.store import PROJECT, Run, Store, TaskReview, content_fingerprint
from apm.runner.agents import AGENTS, build_prompt, revision_prompt

GAPS = {
    "verdict": "incomplete",
    "summary": "Errors aren't handled.",
    "criteria": [
        {"criterion": "It finds titles", "status": "met", "evidence": "search.py:12"},
        {"criterion": "It reports bad queries", "status": "missing", "evidence": ""},
    ],
    "issues": [{"severity": "major", "problem": "No error handling", "where": "search.py"}],
    "follow_ups": [
        {
            "key": "e",
            "title": "Handle errors",
            "kind": "work",
            "intent": "Fail cleanly.",
            "acceptance": ["A bad query gives a clear message"],
            "depends_on_keys": [],
            "depends_on_ids": [],
        }
    ],
}
COMPLETE = {
    "verdict": "complete",
    "summary": "Everything is there now.",
    "criteria": [{"criterion": "It reports bad queries", "status": "met", "evidence": "x.py:3"}],
    "issues": [],
    "follow_ups": [],
}


def child(key: str, title: str, kind: str = "work", **extra: Any) -> dict[str, Any]:
    base = {
        "key": key,
        "title": title,
        "kind": kind,
        "intent": "",
        "acceptance": [],
        "depends_on_keys": [],
        "depends_on_ids": [],
    }
    return base | extra


def turn(
    store: Store,
    node: NodeId,
    agent: str,
    prompt: str,
    *,
    parent: Run | None = None,
    said: Sequence[str] = (),
    events: Sequence[dict[str, Any]] = (),
) -> Run:
    """A finished run, as the runner would have stored it."""
    run = store.create_run(
        node_id=node, agent=agent, prompt=prompt, parent_run=parent.id if parent else None
    )
    for event in events:
        store.add_event(run.id, event)
    for text in said:
        content = [{"type": "text", "text": text}]
        store.add_event(run.id, {"type": "assistant", "message": {"content": content}})
    return store.finish_run(run.id, "succeeded")


def answered(request_id: str, tool: str, kind: str, tool_input: dict, **answer: Any) -> tuple:
    request = {"type": "apm_request", "id": request_id, "tool": tool, "kind": kind}
    resolved = {"type": "apm_request_resolved", "id": request_id, "message": "", "answers": {}}
    return ({**request, "input": tool_input, "description": ""}, {**resolved, **answer})


@dataclass
class Story:
    feature: NodeId
    parse: NodeId
    pick: NodeId
    index: NodeId
    fix: NodeId
    step: NodeId


@pytest.fixture
def story(project: Project) -> Story:
    """How "Search" came to be: planned in a chat about the whole plan, broken down (with
    changes), argued over, partly dropped, built, reviewed twice.
    """
    store = Store(project.apm_dir)
    chat = AGENTS["apm-assistant"]
    graph = load_graph(project)
    first = turn(
        store,
        PROJECT,
        "apm-assistant",
        build_prompt(chat, graph, PROJECT, "Users can't find old notes."),
        said=["Search would fix that. What should it search?"],
    )
    asked = turn(
        store,
        PROJECT,
        "apm-assistant",
        "Titles and bodies. Add it to the plan.",
        parent=first,
        said=["I added Search."],
    )
    body = render_body(intent="Find notes by their words.", acceptance=["It finds titles"])
    (feature,) = create_nodes(
        project, [NewNode(title="Search", body=body)], actor="agent:apm-assistant", run=asked.id
    ).created
    turn(store, PROJECT, "apm-assistant", "Now billing.", parent=asked, said=["Sure."])

    graph = load_graph(project)
    decomposer = AGENTS["apm-decomposer"]
    split = turn(
        store,
        feature,
        "apm-decomposer",
        build_prompt(decomposer, graph, feature, "Three steps at most."),
    )
    turn(store, feature, "apm-critic", "Review this proposed breakdown", parent=split)
    proposed = [
        child("a", "Parse", intent="Read the query.", acceptance=["quotes work"]),
        child("b", "Pick the index", "decision", intent="SQLite FTS or Whoosh?"),
        child("c", "Rank", intent="Order hits."),
    ]
    data = {"rationale": "Parse, then index.", "children": proposed, "warnings": []}
    review = {"verdict": "accept", "summary": "Looks sound."}
    proposal = store.create_proposal(node_id=feature, data=data, run_id=split.id, review=review)
    edited = [
        {**proposed[0], "title": "Parse the query", "acceptance": ["quotes work", "AND works"]},
        proposed[1],
    ]
    parse, pick = accept_proposal(project, store, proposal.id, edited).created
    other = turn(store, feature, "apm-decomposer", build_prompt(decomposer, graph, feature, ""))
    alternative = {"rationale": "Use a server.", "children": [child("x", "Run Elasticsearch")]}
    rejected = store.create_proposal(node_id=feature, data=alternative, run_id=other.id)
    reject_proposal(store, rejected.id)

    edit_body(project, pick, "## Intent\n\nSQLite FTS or Whoosh?\n\n## Notes\n\nSQLite FTS.\n")
    (index,) = create_nodes(project, [NewNode(title="Index", parent=feature)]).created
    set_status(project, index, Status.DROPPED, note="Not needed: SQLite indexes it for us.")

    graph = load_graph(project)
    node = graph[parse]
    fingerprint = content_fingerprint(node, read_body(project.apm_dir, parse))
    store.add_task_review(TaskReview(parse, fingerprint, "pass", {"summary": "About 30 lines."}))
    turn(
        store,
        parse,
        "apm-task-reviewer",
        build_prompt(AGENTS["apm-task-reviewer"], graph, parse, ""),
    )
    question = {"questions": [{"question": "Which logger?", "options": []}]}
    events = (
        *answered(
            "q1",
            "AskUserQuestion",
            "question",
            question,
            allow=True,
            answers={"Which logger?": "structlog"},
        ),
        *answered(
            "p1",
            "Bash",
            "permission",
            {"command": "rm -rf build"},
            allow=False,
            message="Not the build folder.",
        ),
        *answered("p2", "Bash", "permission", {"command": "make test"}, allow=True),
        *answered(
            "p3",
            "Bash",
            "permission",
            {"command": "curl x"},
            allow=False,
            message="Nobody answered at the terminal.",
        ),
    )
    built = turn(
        store,
        parse,
        "apm-implementer",
        build_prompt(AGENTS["apm-implementer"], graph, parse, "Use the new logger."),
        said=["Which logger should I use?", "Done: PR opened."],
        events=events,
    )
    set_status(project, parse, Status.IN_PROGRESS, actor="agent:apm-implementer", run=built.id)

    step = graph.review_step(feature)
    assert step is not None
    target = {"type": "apm_review_target", "branch": "feature/x", "sha": "a" * 40}
    result = {"type": "result", "structured_output": GAPS}
    reviewer = AGENTS["apm-feature-reviewer"]
    first_review = turn(
        store,
        step,
        "apm-feature-reviewer",
        build_prompt(reviewer, graph, step, "Look hard at errors."),
        events=(target, result),
    )
    follow_ups = {"rationale": "Follow-ups.", "children": GAPS["follow_ups"], "warnings": []}
    gaps = store.create_proposal(node_id=feature, data=follow_ups, run_id=first_review.id)
    (fix,) = accept_proposal(project, store, gaps.id).created
    store.add_follow_ups([fix], gaps.id)
    set_status(project, fix, Status.DONE)
    turn(
        store,
        step,
        "apm-feature-reviewer",
        "Look again: the fix is merged.",
        parent=first_review,
        events=({"type": "result", "structured_output": COMPLETE},),
    )
    return Story(feature, parse, pick, index, fix, step)


def record_text(project: Project, story: Story, **kw: Any) -> str:
    changes = BranchChanges(
        "feature/x", "main", "b" * 40, " search.py | 40 +++\n", "b1 Merge #12\n"
    )
    record = gather_record(project, Store(project.apm_dir), story.feature, changes)
    return render_record(record, **kw)


def test_the_record_has_the_feature_its_decisions_and_its_tasks(
    project: Project, story: Story
) -> None:
    text = record_text(project, story)
    assert text.startswith(f"# The record of feature {handle(story.feature)} “Search”")
    assert "> Find notes by their words." in text  # the feature's description, quoted
    description = text.split("### Its description", 1)[1].split("\n## ", 1)[0]
    assert "Notes" not in description  # without the template's empty sections
    assert "Status: dropped.\n\n(no description)" in text  # "Index" has nothing but those
    assert "## Decisions recorded in the plan" in text
    assert f"### {handle(story.pick)} “Pick the index” (todo)" in text
    assert "> SQLite FTS." in text  # the decision's description, quoted
    assert f"{handle(story.parse)} “Parse the query”" in text
    assert "task review: pass: About 30 lines." in text
    assert f"{handle(story.index)} “Index”" in text
    assert "Feature branch: `feature/x`, going into `main`" in text
    assert "search.py | 40 +++" in text  # where to look
    assert "b1 Merge #12" in text


def test_the_record_keeps_what_people_typed_and_chose(project: Project, story: Story) -> None:
    text = record_text(project, story)
    said = text.split("## What people said", 1)[1].split("\n## ", 1)[0]
    # A chat about the whole plan counts only for the turns that changed this feature, and the
    # turn before each.
    assert "Users can't find old notes." in said
    assert "Titles and bodies. Add it to the plan." in said
    assert "Now billing." not in said
    assert "only the turns that changed this feature" in said
    # Guidance given to Decompose, Implement and Review feature, and a reply in a conversation.
    assert "**The user**" in said
    assert "Three steps at most." in said
    assert "Use the new logger." in said
    assert "Look hard at errors." in said
    assert "Look again: the fix is merged." in said
    # Choices made when Claude asked, with Claude's words around them.
    assert "**The user answered**" in said
    assert "“Which logger?”: structlog" in said
    assert "Refused Bash: `rm -rf build`. Reason: Not the build folder." in said
    assert "make test" not in said  # allowing a command decides nothing
    assert "curl x" not in said  # nobody answered: no decision
    assert "Claude: Which logger should I use?" in said
    # APM's own words are not anyone's: the critic, the revision, the prompts around guidance.
    assert "Review this proposed breakdown" not in text
    assert "Decompose node" not in said
    # A conversation where nobody said anything (an automatic task review) is left out.
    assert "Review task on" not in said


def test_the_record_tells_proposals_edits_and_rejections(project: Project, story: Story) -> None:
    text = record_text(project, story)
    proposals = text.split("## Proposals", 1)[1].split("\n## ", 1)[0]
    assert "Breakdown by Decompose for" in proposals
    assert "accepted by the user, after changing it" in proposals
    assert "Rationale: Parse, then index." in proposals
    assert "Reviewer: accept: Looks sound." in proposals
    assert "- [c] Rank (work): Order hits." in proposals  # what was proposed
    assert 'Renamed [a] "Parse" to "Parse the query".' in proposals
    assert 'Added to [a] "Parse" the acceptance criterion “AND works”.' in proposals
    assert 'Left out [c] "Rank".' in proposals
    assert "Pick the index" in proposals.split("Became:", 1)[1]
    # A rejected alternative, and follow-ups APM accepted on its own (not the user's decision).
    assert "rejected by the user" in proposals
    assert "Run Elasticsearch" in proposals
    assert "accepted by APM on its own (automation.accept_follow_ups)" in proposals


def test_the_record_has_the_plan_history_with_who_did_what(project: Project, story: Story) -> None:
    text = record_text(project, story)
    history = text.split("## The plan's history", 1)[1].split("\n## ", 1)[0]
    assert "Claude in a chat, as asked there · create" in history
    assert "the user, accepting apm-decomposer's proposal · decompose" in history
    assert (
        f"the user · status {handle(story.index)}: dropped: Not needed: SQLite indexes it"
        in history
    )
    assert f"the user · edit-body {handle(story.pick)}" in history
    # Follow-ups APM accepted on its own are nobody's decision.
    assert "APM on its own, accepting apm-feature-reviewer's proposal · decompose" in history
    assert "(1 more commit was APM's and the agents' own bookkeeping" in history


def test_the_record_has_every_review_turn_and_its_follow_ups(
    project: Project, story: Story
) -> None:
    text = record_text(project, story)
    reviews = text.split("## Feature reviews", 1)[1].split("\n## ", 1)[0]
    first, second = reviews.split("### Turn 2")
    assert "### Turn 1: " in first
    assert "`feature/x` at `aaaaaaa` · Incomplete" in first
    assert "- Missing: It reports bad queries" in first
    assert "- major: No error handling (search.py)" in first
    assert f"Its follow-up tasks: {handle(story.fix)} “Handle errors” (done)" in first
    assert "· Complete" in second  # the reply's verdict, on the same commit
    assert "Everything is there now." in second


def test_a_long_record_drops_claude_s_replies_but_never_a_typed_message(project: Project) -> None:
    store = Store(project.apm_dir)
    (feature,) = create_nodes(project, [NewNode(title="Big")]).created
    create_nodes(project, [NewNode(title="Part", parent=feature)])
    chat = AGENTS["apm-assistant"]
    graph = load_graph(project)
    previous = None
    long_message = "Keep this. " + "x" * 5_000
    for number in range(30):
        text = long_message if number == 0 else f"Message {number}: keep it."
        prompt = build_prompt(chat, graph, feature, text) if previous is None else text
        previous = turn(
            store, feature, "apm-assistant", prompt, parent=previous, said=["y" * 4_000]
        )
    record = gather_record(project, store, feature)

    roomy = render_record(record, limit=1_000_000)
    assert "y" * 800 in roomy  # replies shortened to their end
    assert "y" * 801 not in roomy

    tight = render_record(record, limit=10_000)
    assert len(tight) <= 10_000
    assert "Claude's replies are left out" in tight
    assert "Claude: " not in tight
    for number in range(1, 30):
        assert f"Message {number}: keep it." in tight
    assert "Keep this. xxx" in tight  # shortened, not dropped
    assert "more characters cut" in tight


@pytest.mark.parametrize(
    "agent",
    [
        "apm-decomposer",
        "apm-implementer",
        "apm-feature-reviewer",
        "apm-task-reviewer",
        "apm-feature-reporter",
    ],
)
def test_typed_text_reads_the_real_prompts(project: Project, agent: str) -> None:
    (feature,) = create_nodes(project, [NewNode(title="Feature")]).created
    graph = load_graph(project)
    spec = AGENTS[agent]

    def run(prompt: str, parent: str | None = None) -> Run:
        return Run(
            "r", feature, agent, "s", "succeeded", 0.0, None, None, None, prompt, parent, None
        )

    typed = typed_text(run(build_prompt(spec, graph, feature, "Mind the cache.\nSecond line.")))
    if agent == "apm-feature-reporter":
        assert typed is None  # guidance about the report says nothing about the feature
        return
    assert typed == "Mind the cache.\nSecond line."
    assert typed_text(run(build_prompt(spec, graph, feature, ""))) is None
    assert typed_text(run("Look again.", parent="r0")) == "Look again."


def test_typed_text_of_a_chat_and_of_apm_s_own_turns(project: Project) -> None:
    (node,) = create_nodes(project, [NewNode(title="Feature")]).created
    graph = load_graph(project)
    chat = AGENTS["apm-assistant"]

    def run(agent: str, prompt: str, node_id: str = node) -> Run:
        return Run("r", node_id, agent, "s", "succeeded", 0.0, None, None, None, prompt, None, None)

    assert (
        typed_text(run("apm-assistant", build_prompt(chat, graph, node, "Why?\n\nBecause.")))
        == "Why?\n\nBecause."
    )
    assert typed_text(run("apm-assistant", build_prompt(chat, graph, node, ""))) is None
    assert typed_text(run("apm-assistant", build_prompt(chat, graph, PROJECT, ""), PROJECT)) is None
    assert (
        typed_text(run("apm-assistant", build_prompt(chat, graph, PROJECT, "Hi"), PROJECT)) == "Hi"
    )
    assert typed_text(run("apm-decomposer", revision_prompt({"verdict": "revise"}))) is None
    assert typed_text(run("apm-critic", "anything")) is None


def test_proposal_changes_name_every_difference() -> None:
    proposed = [
        child("a", "A", intent="Old.", acceptance=["one"], depends_on_keys=["b"]),
        child("b", "B"),
    ]
    assert proposal_changes(proposed, None) == []
    assert proposal_changes(proposed, proposed) == []
    accepted = [
        child("a", "A2", "decision", intent="New.", acceptance=["one ", "two"]),
        child("n", "New one"),
    ]
    assert proposal_changes(proposed, accepted) == [
        'Renamed [a] "A" to "A2".',
        'Made [a] "A" a decision instead of a work.',
        'Changed the intent of [a] "A" from “Old.” to “New.”.',
        'Added to [a] "A" the acceptance criterion “two”.',
        'Removed from [a] "A" the dependency on “b”.',
        'Left out [b] "B".',
        'Added [n] "New one", which wasn\'t proposed.',
    ]


@pytest.mark.parametrize("detail", ["brief", "standard", "detailed"])
def test_the_detail_level_sets_what_the_reporter_is_asked_for(
    project: Project, detail: Any
) -> None:
    (feature,) = create_nodes(project, [NewNode(title="Search")]).created
    prompt = report_prompt(
        load_graph(project),
        feature,
        branch="feature/x",
        sha="c" * 40,
        home="main",
        path="docs/features/search-x.md",
        detail=detail,
        record="# The record",
        message="Keep it short.",
    )
    assert f"at **{detail}** detail" in prompt
    assert OUTLINES[detail] in prompt
    assert ("Implementation notes" in prompt) == (detail == "detailed")
    assert ("alternatives that were" in prompt) == (detail != "brief")
    assert "never invent a rationale" in prompt
    assert '"No reason was recorded."' in prompt
    assert "## Guidance from the user\n\nKeep it short." in prompt
    assert prompt.index("# The record") > prompt.index("## Rules")
    assert "`git diff origin/main...HEAD`" in prompt


def test_the_answer_and_the_file() -> None:
    answer = parse_answer({"summary": "Search.", "markdown": "# Search\n\nIt finds notes.\n"})
    feature = "0192f0c1-8a2b-7c3d-9e4f-1c0b9a44d2e7"
    text = report_file(answer, feature, "brief", datetime(2026, 11, 22, tzinfo=UTC))
    assert text.startswith("# Search\n\nIt finds notes.\n\n---\n\n")
    assert "feature `9a44d2e7`, at brief detail, on 2026-11-22" in text
    with pytest.raises(ReportRefused, match="not a valid report"):
        parse_answer({"summary": "x"})
    with pytest.raises(ReportRefused, match="markdown is empty"):
        parse_answer({"summary": "x", "markdown": "  \n"})
    assert ReportAnswer(markdown="# x").summary == ""


def test_a_record_with_nothing_said_says_so(project: Project) -> None:
    (feature,) = create_nodes(project, [NewNode(title="Quiet")]).created
    create_nodes(project, [NewNode(title="Part", parent=feature, kind=Kind.WORK)])
    text = render_record(gather_record(project, Store(project.apm_dir), feature))
    assert "No conversation about this feature has anything a person typed or chose." in text
    assert "## Proposals" not in text
    assert "## Feature reviews" not in text


def test_a_task_split_in_place_stays_in_its_features_record(project: Project) -> None:
    store = Store(project.apm_dir)
    (feature,) = create_nodes(project, [NewNode(title="Cost view")]).created
    (task,) = create_nodes(project, [NewNode(title="Ledger", parent=feature)]).created
    asked = turn(store, task, "apm-assistant", "Keep the ledger per agent, please.")
    review = turn(store, task, "apm-task-reviewer", "Review the task.")
    children = [
        {"key": "a", "title": "Sum runs", "intent": "Sum them."},
        {"key": "b", "title": "Freeze", "intent": "Freeze it.", "depends_on_keys": ["a"]},
    ]
    data = {"rationale": "Two parts.", "children": children, "split_of": {"title": "Ledger"}}
    proposal = store.create_proposal(node_id=task, data=data, run_id=review.id)
    accept_proposal(project, store, proposal.id)
    assert task not in load_graph(project)

    record = gather_record(project, store, feature)
    (item,) = record.proposals
    assert item.where == f"{handle(task)} “Ledger” (replaced by its split)"
    assert len(item.became) == 2
    assert any(asked.prompt in line.text for c in record.conversations for line in c.lines)
