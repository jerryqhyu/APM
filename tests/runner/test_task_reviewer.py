"""The task reviewer: registered read-only, with a prompt, and refused where it doesn't apply."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from apm.core.config import read_config
from apm.core.files import read_body
from apm.core.ids import handle
from apm.core.model import NodeId
from apm.core.ops import NewNode, create_nodes, edit_body
from apm.core.project import Project, load_graph
from apm.core.proposals import accept_proposal
from apm.core.review_lock import TaskUnderReview, review_lock_path, take_review_lock, under_review
from apm.core.store import Store, content_fingerprint
from apm.core.task_verdict import TASK_VERDICT_SCHEMA
from apm.runner.agents import AGENTS, EDIT_TOOLS, RunRefused, build_command, build_prompt
from apm.runner.runner import Runner
from tests.core.test_feature_review import feature
from tests.runner.fake import FakeClaude
from tests.runner.test_runner import Harness, option

SPEC = AGENTS["apm-task-reviewer"]


def test_it_is_registered_read_only(project: Project) -> None:
    assert SPEC.read_only
    assert SPEC.user_startable
    assert SPEC.scope == "read"
    assert SPEC.schema is TASK_VERDICT_SCHEMA
    assert not set(SPEC.tools) & set(EDIT_TOOLS)
    command = build_command(
        SPEC,
        read_config(project.apm_dir),
        mcp_config_path=Path("m.json"),
        session_id="s",
        mode="auto",
    )
    assert option({"argv": command}, "--disallowedTools") == ",".join(EDIT_TOOLS)
    assert "--json-schema" in command


def test_its_prompt_names_the_task_and_the_skill(project: Project) -> None:
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    prompt = build_prompt(SPEC, load_graph(project), task, "  be strict ")
    assert f'node {handle(task)} ("Parse lines")' in prompt
    assert "apm-task-sizing" in prompt
    assert prompt.endswith("Guidance from the user:\nbe strict")


async def test_it_reviews_a_task(project: Project, fake_claude: FakeClaude) -> None:
    harness = Harness(project)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    run = harness.runner.start("apm-task-reviewer", task)
    await harness.runner.wait(run.id)
    assert harness.store.get_run(run.id).status == "succeeded"
    (call,) = fake_claude.calls
    assert call["agent"] == "apm-task-reviewer"


async def test_it_is_refused_on_a_review_step_or_a_parent(project: Project) -> None:
    harness = Harness(project)
    node, step, _tasks = feature(project, "First", "Second")
    with pytest.raises(RunRefused, match="this is a feature's review step"):
        harness.runner.start("apm-task-reviewer", step)
    with pytest.raises(RunRefused, match="only a task can be reviewed: this node has children"):
        harness.runner.start("apm-task-reviewer", node)
    assert harness.store.list_runs() == []


def fingerprint(project: Project, task: NodeId) -> str:
    return content_fingerprint(load_graph(project)[task], read_body(project.apm_dir, task))


async def review(
    project: Project, fake_claude: FakeClaude, *modes: str
) -> tuple[Harness, NodeId, str]:
    fake_claude.mode(*modes)
    harness = Harness(project)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    run = harness.runner.start("apm-task-reviewer", task)
    await harness.runner.wait(run.id)
    return harness, task, run.id


async def test_a_pass_is_stored_for_the_content_it_reviewed(
    project: Project, fake_claude: FakeClaude
) -> None:
    harness, task, run_id = await review(project, fake_claude)
    stored = harness.store.latest_task_review(task)
    assert stored is not None
    assert (stored.verdict, stored.run_id) == ("pass", run_id)
    assert stored.fingerprint == fingerprint(project, task)
    assert stored.data["summary"] == "About 20 lines."
    assert harness.notices(run_id) == [
        ("info", "**Pass**: the task is small enough to build. About 20 lines.")
    ]
    assert harness.store.list_proposals() == []


async def test_the_fingerprint_is_taken_when_the_run_starts(
    project: Project, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("ask-permission")
    harness = Harness(project)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    before = fingerprint(project, task)
    run = harness.runner.start("apm-task-reviewer", task)
    request = await harness.next_request()
    # The review lock refuses edits meanwhile, so the verdict is for exactly what was shown.
    with pytest.raises(TaskUnderReview):
        edit_body(project, task, "## Intent\n\nSomething else entirely.\n")
    await harness.runner.answer(request["id"], allow=True)
    await harness.runner.wait(run.id)
    stored = harness.store.latest_task_review(task)
    assert stored is not None
    assert stored.fingerprint == before == fingerprint(project, task)


async def test_too_big_proposes_a_split_that_turns_the_task_into_a_feature(
    project: Project, fake_claude: FakeClaude
) -> None:
    harness, task, run_id = await review(project, fake_claude, "too-big")
    stored = harness.store.latest_task_review(task)
    assert stored is not None
    assert stored.verdict == "too_big"
    (proposal,) = harness.store.list_proposals()
    assert (proposal.node_id, proposal.status, proposal.run_id) == (task, "pending", run_id)
    assert "proposal.created" in harness.types()
    ((level, text),) = harness.notices(run_id)
    assert level == "warning"
    assert "A split into 2 tasks waits under Proposals" in text

    accept_proposal(project, harness.store, proposal.id)
    graph = load_graph(project)
    children = graph.children(task)
    assert sorted(graph[c].title for c in children) == ["Feature review", "Parse", "Write"]
    step = graph.review_step(task)
    assert step is not None
    assert set(graph[step].depends_on) == set(children) - {step}


async def test_unclear_says_what_is_missing(project: Project, fake_claude: FakeClaude) -> None:
    harness, task, run_id = await review(project, fake_claude, "unclear")
    stored = harness.store.latest_task_review(task)
    assert stored is not None
    assert (stored.verdict, stored.data["missing"]) == ("unclear", ["the format"])
    assert harness.notices(run_id) == [("warning", "**Unclear**: Too vague. Missing: the format")]
    assert harness.store.list_proposals() == []


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("bad-verdict", "too_big needs at least two proposed child tasks"),
        ("fail", "boom: something broke"),
        ("no-answer", "no structured answer"),
    ],
)
async def test_malformed_output_or_a_failed_run_is_stored_as_an_error(
    project: Project, fake_claude: FakeClaude, mode: str, reason: str
) -> None:
    harness, task, run_id = await review(project, fake_claude, mode)
    stored = harness.store.latest_task_review(task)
    assert stored is not None
    assert (stored.verdict, stored.fingerprint) == ("error", fingerprint(project, task))
    assert reason in stored.data["error"]
    assert harness.store.get_run(run_id).status == "failed"
    ((level, text),) = harness.notices(run_id)
    assert level == "warning"
    assert reason in text
    assert harness.store.list_proposals() == []


async def test_it_holds_the_lock_until_it_succeeds(
    project: Project, fake_claude: FakeClaude
) -> None:
    harness = Harness(project)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    run = harness.runner.start("apm-task-reviewer", task)
    assert under_review(project.apm_dir, task)
    await harness.runner.wait(run.id)
    assert harness.store.get_run(run.id).status == "succeeded"
    assert not under_review(project.apm_dir, task)


async def test_it_releases_the_lock_when_it_fails(
    project: Project, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("fail")
    harness = Harness(project)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    run = harness.runner.start("apm-task-reviewer", task)
    assert under_review(project.apm_dir, task)
    await harness.runner.wait(run.id)
    assert harness.store.get_run(run.id).status == "failed"
    assert not under_review(project.apm_dir, task)


async def test_it_releases_the_lock_when_cancelled(
    project: Project, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("hang")
    harness = Harness(project)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    run = harness.runner.start("apm-task-reviewer", task)
    while not harness.store.list_events(run.id):  # wait until it is really running
        await asyncio.sleep(0.02)
    assert under_review(project.apm_dir, task)
    await asyncio.wait_for(harness.runner.cancel(run.id), 10)
    assert harness.store.get_run(run.id).status == "cancelled"
    assert not under_review(project.apm_dir, task)


@pytest.mark.parametrize("agent", ["apm-implementer", "apm-decomposer"])
async def test_building_a_task_under_review_is_refused(project: Project, agent: str) -> None:
    harness = Harness(project)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    take_review_lock(project.apm_dir, task, "some-run")
    with pytest.raises(RunRefused, match=f"{handle(task)} is under review"):
        harness.runner.start(agent, task)
    assert harness.store.list_runs() == []


def test_a_restart_releases_the_lock_of_a_killed_review(project: Project) -> None:
    store = Store(project.apm_dir)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    dead = 2**22 + 12345
    orphan = store.create_run(node_id=task, agent="apm-task-reviewer", pid=dead)
    take_review_lock(project.apm_dir, task, orphan.id, pid=dead)
    Runner(project, store)  # `apm serve` starting again
    assert not review_lock_path(project.apm_dir, task).exists()
    assert store.get_run(orphan.id).status == "failed"
    review = store.latest_task_review(task)
    assert review is not None
    assert (review.verdict, review.run_id) == ("error", orphan.id)


def test_a_restart_leaves_a_lock_another_run_holds(project: Project) -> None:
    store = Store(project.apm_dir)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    orphan = store.create_run(node_id=task, agent="apm-task-reviewer", pid=2**22 + 12345)
    take_review_lock(project.apm_dir, task, "a-live-run")
    Runner(project, store)
    assert under_review(project.apm_dir, task)
    assert store.get_run(orphan.id).status == "failed"


@pytest.mark.parametrize("mode", ["", "fail", "bad-verdict"])
async def test_the_run_finishes_after_the_verdict_is_stored_and_the_lock_released(
    project: Project, fake_claude: FakeClaude, mode: str
) -> None:
    fake_claude.mode(mode)
    harness = Harness(project)
    task = create_nodes(project, [NewNode(title="Parse lines")]).created[0]
    seen: list[tuple[str, bool, str | None]] = []

    def on_event(event: dict[str, Any]) -> None:
        if event["type"] == "run.finished":
            stored = harness.store.latest_task_review(task)
            status = harness.store.get_run(event["run"]["id"]).status
            seen.append(
                (status, under_review(project.apm_dir, task), stored.verdict if stored else None)
            )

    runner = Runner(project, harness.store, on_event)
    run = runner.start("apm-task-reviewer", task)
    await runner.wait(run.id)
    expected = ("succeeded", False, "pass") if mode == "" else ("failed", False, "error")
    assert seen == [expected]
