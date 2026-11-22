"""The task size limit from apm.yaml reaches the agents that size, split and build tasks."""

from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.settings import set_defaults, set_override
from apm.runner.agents import AGENTS, build_prompt
from tests.runner.fake import FakeClaude
from tests.runner.test_runner import Harness


def feature_with_a_task(project: Project) -> tuple[str, str]:
    top = create_nodes(project, [NewNode(title="Search")]).created[0]
    task = create_nodes(project, [NewNode(title="Index", parent=top)]).created[0]
    return top, task


def test_without_a_config_prompts_use_the_default_limit(project: Project) -> None:
    _, task = feature_with_a_task(project)
    prompt = build_prompt(AGENTS["apm-task-reviewer"], load_graph(project), task, "")
    assert prompt.endswith("The limit for this task is 50 real lines of code.")


async def test_the_task_reviewer_is_told_the_limit_for_its_task(
    project: Project, fake_claude: FakeClaude
) -> None:
    top, task = feature_with_a_task(project)
    set_defaults(project, features={"task_max_lines": 80})
    other = create_nodes(project, [NewNode(title="Elsewhere")]).created[0]
    set_override(project, top, {"task_max_lines": 30})
    harness = Harness(project)
    for node in (task, other):
        run = harness.runner.start("apm-task-reviewer", node)
        await harness.runner.wait(run.id)
    inside, outside = (call["prompt"] for call in fake_claude.calls)
    assert "The limit for this task is 30 real lines of code." in inside
    assert "The limit for this task is 80 real lines of code." in outside


async def test_the_decomposer_sizes_its_tasks_to_the_limit(
    project: Project, fake_claude: FakeClaude
) -> None:
    top, _ = feature_with_a_task(project)
    set_override(project, top, {"task_max_lines": 40})
    harness = Harness(project)
    run = harness.runner.start("apm-decomposer", top, critic=False)
    await harness.runner.wait(run.id)
    (call,) = fake_claude.calls
    assert "the task size limit here, 40 real lines of code each" in call["prompt"]


async def test_the_implementer_aims_for_the_limit(
    project: Project, fake_claude: FakeClaude
) -> None:
    top, task = feature_with_a_task(project)
    set_override(project, top, {"task_max_lines": 35})
    harness = Harness(project)
    harness.review(task)
    run = harness.runner.start("apm-implementer", task)
    await harness.runner.wait(run.id)
    (call,) = fake_claude.calls
    assert "Aim to keep the change within 35 real lines of code" in call["prompt"]
    assert "Aim to keep the change within 35" in harness.store.get_run(run.id).prompt
