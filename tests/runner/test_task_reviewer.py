"""The task reviewer: registered read-only, with a prompt, and refused where it doesn't apply."""

from pathlib import Path

import pytest

from apm.core.config import read_config
from apm.core.ids import handle
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.task_verdict import TASK_VERDICT_SCHEMA
from apm.runner.agents import AGENTS, EDIT_TOOLS, RunRefused, build_command, build_prompt
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
