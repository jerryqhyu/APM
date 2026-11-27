"""The auto-run queue: task reviews start on their own, debounced, within `max_concurrent`."""

import asyncio

import pytest

from apm.core.model import NodeId, Status
from apm.core.ops import NewNode, create_nodes, set_status, update_node
from apm.core.project import Project, load_graph
from apm.runner import auto
from apm.runner.auto import TASK_REVIEWER, AutoQueue
from apm.runner.runner import FEATURE_REVIEWER, RunnerBusy
from tests.conftest import configure
from tests.runner.fake import FakeClaude
from tests.runner.test_runner import Harness, option


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def tasks(project: Project, *titles: str) -> tuple[NodeId, ...]:
    return create_nodes(project, [NewNode(title=t) for t in titles]).created


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
    await harness.settle()
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
    await harness.settle()
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
        await harness.settle()
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
    await harness.settle()
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


def test_reconcile_runs_every_interval_and_not_when_off(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(project, "automation:\n  reconcile_interval_s: 60\n")
    calls: list[bool] = []
    monkeypatch.setattr(auto, "reconcile", lambda _, *, actor, retarget: calls.append(retarget))
    clock = Clock()
    queue = AutoQueue(Harness(project).runner, clock=clock)
    ran = []
    for now in (0, 10, 59, 60, 100, 120):
        clock.now = now
        ran.append(queue.reconcile())
    assert ran == [True, False, False, True, False, True]
    assert calls == [True, True, True]
    configure(project, "  reconcile: false\n")
    clock.now += 1000
    assert not queue.reconcile()
    assert len(calls) == 3


def feature(project: Project, *titles: str) -> tuple[NodeId, NodeId, tuple[NodeId, ...]]:
    """A feature with done tasks: its review step is ready."""
    (top,) = tasks(project, "Feature")
    kids = create_nodes(project, [NewNode(title=t, parent=top) for t in titles]).created
    for kid in kids:
        set_status(project, kid, Status.DONE)
    step = load_graph(project).review_step(top)
    assert step is not None
    return top, step, kids


def recording(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, NodeId]]:
    started: list[tuple[str, NodeId]] = []
    monkeypatch.setattr(harness.runner, "start", lambda a, n, **_: started.append((a, n)))
    return started


def test_a_ready_review_step_queues_one_feature_review(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(project, "automation:\n  task_review: false\n")
    _, step, _ = feature(project, "Parse", "Print")
    harness = Harness(project)
    started = recording(harness, monkeypatch)
    queue = AutoQueue(harness.runner, debounce_s=0)
    for _ in range(3):
        queue.scan()
        queue.pump()
    assert started == [(FEATURE_REVIEWER, step)]


def test_feature_review_off_queues_none(project: Project) -> None:
    configure(project, "automation:\n  task_review: false\n  feature_review: false\n")
    feature(project, "Parse")
    queue = AutoQueue(Harness(project).runner, debounce_s=0)
    queue.scan()
    assert queue.queued == {}


def test_a_busy_review_step_stays_queued(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(project, "automation:\n  task_review: false\n")
    _, step, _ = feature(project, "Parse")
    harness = Harness(project)

    def busy(*_: object, **__: object) -> None:
        raise RunnerBusy("busy")

    monkeypatch.setattr(harness.runner, "start", busy)
    queue = AutoQueue(harness.runner, debounce_s=0)
    queue.scan()
    queue.pump()
    assert (FEATURE_REVIEWER, step) in queue.queued


def test_the_review_is_queued_again_once_reconcile_finishes_the_follow_ups(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(project, "automation:\n  task_review: false\n")
    top, step, _ = feature(project, "Parse")
    harness = Harness(project)
    started = recording(harness, monkeypatch)
    queue = AutoQueue(harness.runner, debounce_s=0)
    queue.scan()
    queue.pump()
    # The review found a gap: a follow-up task, and the step goes back to do.
    set_status(project, step, Status.IN_PROGRESS)
    (follow_up,) = create_nodes(project, [NewNode(title="Fix", parent=top)]).created
    set_status(project, step, Status.TODO)
    queue.scan()
    queue.pump()
    assert started == [(FEATURE_REVIEWER, step)]
    monkeypatch.setattr(auto, "reconcile", lambda p, **_: set_status(p, follow_up, Status.DONE))
    assert queue.reconcile()
    queue.scan()
    queue.pump()
    assert started == [(FEATURE_REVIEWER, step), (FEATURE_REVIEWER, step)]


async def cycle(queue: AutoQueue, harness: Harness, clock: Clock, times: int = 3) -> None:
    for _ in range(times):
        clock.now += 10
        queue.scan()
        queue.pump()
        await harness.settle()


async def test_an_errored_task_review_is_retried_once_without_a_restart(
    project: Project, fake_claude: FakeClaude
) -> None:
    (task,) = tasks(project, "Parse")
    harness, clock = Harness(project), Clock()
    queue = AutoQueue(harness.runner, debounce_s=0, clock=clock)
    fake_claude.mode("fail")
    await cycle(queue, harness, clock, times=5)
    assert [r.status for r in harness.store.list_runs()] == ["failed", "failed"]
    assert queue.queued == {}
    # A review started by hand after two errors still runs.
    by_hand = harness.runner.start(TASK_REVIEWER, task)
    await harness.runner.wait(by_hand.id)
    assert len(harness.store.list_runs()) == 3
    await cycle(queue, harness, clock)
    assert len(harness.store.list_runs()) == 3
    # An edit is new content: it gets a review of its own.
    update_node(project, task, title="Parse the input")
    await cycle(queue, harness, clock, times=5)
    runs = harness.store.list_runs()
    assert len(runs) == 5
    assert '("Parse the input")' in fake_claude.calls[-1]["prompt"]


async def test_a_successful_task_review_is_not_run_again(
    project: Project, fake_claude: FakeClaude
) -> None:
    tasks(project, "Parse")
    harness, clock = Harness(project), Clock()
    queue = AutoQueue(harness.runner, debounce_s=0, clock=clock)
    await cycle(queue, harness, clock, times=5)
    assert [r.status for r in harness.store.list_runs()] == ["succeeded"]


async def test_drive_rescans_when_a_run_finishes(
    project: Project, fake_claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks(project, "Parse")
    harness = Harness(project)
    events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    monkeypatch.setattr(harness.runner, "_on_event", events.put_nowait)
    fake_claude.mode("fail")
    queue = AutoQueue(harness.runner, debounce_s=0)
    driver = asyncio.create_task(queue.drive(events, tick_s=60))  # only events wake it
    for _ in range(500):
        if len(harness.store.list_runs(status="failed")) == 2:
            break
        await asyncio.sleep(0.01)
    driver.cancel()
    await harness.settle()
    assert len(harness.store.list_runs()) == 2
