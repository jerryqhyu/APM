"""The task reviewer: registered read-only, with a prompt, and refused where it doesn't apply."""

import asyncio
from pathlib import Path

import pytest

from apm.core.config import read_config
from apm.core.ids import handle
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.review_lock import review_lock_path, take_review_lock, under_review
from apm.core.store import Store
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
