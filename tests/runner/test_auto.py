"""The auto-run queue: task reviews start on their own, debounced, within `max_concurrent`."""

import asyncio

from apm.core.model import NodeId, Status
from apm.core.ops import NewNode, create_nodes, set_status, update_node
from apm.core.project import Project
from apm.runner.auto import TASK_REVIEWER, AutoQueue
from tests.runner.fake import FakeClaude
from tests.runner.test_runner import Harness, option


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def configure(project: Project, text: str) -> None:
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + text)


def tasks(project: Project, *titles: str) -> tuple[NodeId, ...]:
    return create_nodes(project, [NewNode(title=t) for t in titles]).created


async def settle(harness: Harness) -> None:
    for run in harness.store.list_runs(status="running"):
        await harness.runner.wait(run.id)


async def test_drive_queues_every_due_task_at_startup(
    project: Project, fake_claude: FakeClaude
) -> None:
    first, second, done = tasks(project, "First", "Second", "Done already")
    set_status(project, done, Status.DONE)
    harness = Harness(project)
    queue = AutoQueue(harness.runner, debounce_s=0)
    driver = asyncio.create_task(queue.drive(asyncio.Queue(), tick_s=0.01))
    for _ in range(500):
        if len(harness.store.list_runs()) == 2:
            break
        await asyncio.sleep(0.01)
    driver.cancel()
    await settle(harness)
    runs = harness.store.list_runs()
    assert sorted(r.node_id for r in runs) == sorted([first, second])
    assert {r.agent for r in runs} == {TASK_REVIEWER}


async def test_a_burst_of_edits_gives_one_run_on_the_final_content(
    project: Project, fake_claude: FakeClaude
) -> None:
    (task,) = tasks(project, "Draft 0")
    harness, clock = Harness(project), Clock()
    queue = AutoQueue(harness.runner, debounce_s=3, clock=clock)
    queue.scan()
    for n in range(1, 6):
        clock.now += 1  # each edit lands inside the window of the one before
        update_node(project, task, title=f"Draft {n}")
        queue.scan()
        queue.pump()
        assert harness.store.list_runs() == []
    clock.now += 3
    queue.pump()
    await settle(harness)
    for _ in range(3):  # rescans of the same content start nothing new
        clock.now += 10
        queue.scan()
        queue.pump()
    (call,) = fake_claude.calls
    assert '("Draft 5")' in call["prompt"]
    assert len(harness.store.list_runs()) == 1


async def test_queued_runs_wait_for_room_under_max_concurrent(
    project: Project, fake_claude: FakeClaude
) -> None:
    configure(project, "runner:\n  max_concurrent: 1\n")
    tasks(project, "One", "Two", "Three")
    harness = Harness(project)
    queue = AutoQueue(harness.runner, debounce_s=0)
    queue.scan()
    for _ in range(3):
        queue.pump()
        assert harness.runner.active() == 1
        assert len(queue.queued) == 3 - len(harness.store.list_runs())
        await settle(harness)
    runs = harness.store.list_runs()
    assert len(runs) == 3
    assert {r.status for r in runs} == {"succeeded"}
    assert queue.queued == {}


async def test_a_busy_node_stays_queued(project: Project, fake_claude: FakeClaude) -> None:
    (task,) = tasks(project, "Parse")
    harness = Harness(project)
    fake_claude.mode("hang")
    by_hand = harness.runner.start(TASK_REVIEWER, task)
    queue = AutoQueue(harness.runner, debounce_s=0)
    queue.scan()
    queue.pump()
    assert (TASK_REVIEWER, task) in queue.queued
    await harness.runner.cancel(by_hand.id)
    assert [r.id for r in harness.store.list_runs()] == [by_hand.id]


async def test_runs_use_the_agents_budget(project: Project, fake_claude: FakeClaude) -> None:
    configure(project, "runner:\n  agents:\n    apm-task-reviewer: { budget_usd: 0.75 }\n")
    tasks(project, "Parse")
    harness = Harness(project)
    queue = AutoQueue(harness.runner, debounce_s=0)
    queue.scan()
    queue.pump()
    await settle(harness)
    (call,) = fake_claude.calls
    assert option(call, "--max-budget-usd") == "0.75"


def test_switching_task_review_off_stops_queueing(project: Project) -> None:
    (task,) = tasks(project, "Parse")
    queue = AutoQueue(Harness(project).runner)
    queue.scan()
    assert list(queue.queued) == [(TASK_REVIEWER, task)]
    configure(project, "automation:\n  task_review: false\n")
    queue.scan()
    assert queue.queued == {}
