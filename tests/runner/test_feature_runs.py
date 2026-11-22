"""Review all tasks and Implement all: every task of a feature, within `max_concurrent`."""

import asyncio

import pytest

from apm.core.ids import handle
from apm.core.model import NodeId, Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, delete_node, set_status
from apm.core.project import Project, load_graph
from apm.core.review_lock import take_review_lock
from apm.runner.agents import RunRefused
from apm.runner.auto import TASK_REVIEWER, AutoQueue
from apm.runner.feature_runs import (
    IMPLEMENTER,
    Batch,
    Skipped,
    implement_feature,
    review_all_tasks,
    tasks_in,
)
from apm.runner.queue import Queued, RunQueue
from tests.runner.fake import FakeClaude
from tests.runner.test_runner import Harness


def configure(project: Project, text: str) -> None:
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + text)


def add(project: Project, title: str, parent: NodeId | None = None, *deps: NodeId) -> NodeId:
    new = NewNode(title=title, parent=parent, depends_on=deps)
    return create_nodes(project, [new]).created[0]


async def finish(harness: Harness, queue: RunQueue) -> None:
    """Pumps the queue as runs finish, until nothing is running or waiting."""
    for _ in range(2000):
        queue.pump()
        if not queue.waiting and not harness.runner.active():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("runs did not finish")


def reviewed(harness: Harness) -> list[NodeId]:
    runs = harness.store.list_runs()
    return sorted(r.node_id for r in runs if r.agent == TASK_REVIEWER)


@pytest.fixture
def plan(project: Project) -> dict[str, NodeId]:
    """A feature with two tasks and a nested feature with two more, one of them done."""
    feature = add(project, "Search")
    first = add(project, "Parse", feature)
    second = add(project, "Index", feature)
    nested = add(project, "Ranking", feature)
    third = add(project, "Score", nested)
    done = add(project, "Sort", nested)
    set_status(project, done, Status.DONE)
    return {
        "feature": feature,
        "first": first,
        "second": second,
        "nested": nested,
        "third": third,
        "done": done,
    }


def test_the_tasks_are_the_to_do_leaves_without_review_steps(
    project: Project, plan: dict[str, NodeId]
) -> None:
    graph = load_graph(project)
    expected = [plan["first"], plan["second"], plan["third"]]
    assert tasks_in(graph, plan["feature"]) == expected
    assert tasks_in(graph, plan["first"]) == [plan["first"]]
    assert tasks_in(graph, plan["done"]) == []
    step = graph.review_step(plan["feature"])
    assert step is not None
    assert tasks_in(graph, step) == []


# --- Review all tasks ---


async def test_review_all_tasks_starts_what_fits_and_queues_the_rest(
    project: Project, plan: dict[str, NodeId], fake_claude: FakeClaude
) -> None:
    configure(project, "runner:\n  max_concurrent: 1\n")
    harness = Harness(project)
    queue = RunQueue(harness.runner)
    batch = review_all_tasks(queue, plan["feature"])
    assert [r.node_id for r in batch.started] == [plan["first"]]
    assert batch.waiting == (plan["second"], plan["third"])
    assert batch.skipped == ()
    await finish(harness, queue)
    assert reviewed(harness) == sorted([plan["first"], plan["second"], plan["third"]])
    assert {r.status for r in harness.store.list_runs()} == {"succeeded"}
    graph = load_graph(project)
    assert all(not graph.is_review(r.node_id) for r in harness.store.list_runs())


async def test_review_all_tasks_skips_tasks_under_review(
    project: Project, plan: dict[str, NodeId], fake_claude: FakeClaude
) -> None:
    take_review_lock(project.apm_dir, plan["second"], "someone-else")
    harness = Harness(project)
    queue = RunQueue(harness.runner)
    batch = review_all_tasks(queue, plan["feature"])
    assert batch.skipped == (Skipped(plan["second"], "already under review"),)
    assert sorted(r.node_id for r in batch.started) == sorted([plan["first"], plan["third"]])
    await finish(harness, queue)
    assert reviewed(harness) == sorted([plan["first"], plan["third"]])


async def test_review_all_tasks_reviews_passed_tasks_again(
    project: Project, plan: dict[str, NodeId], fake_claude: FakeClaude
) -> None:
    harness = Harness(project)
    harness.review(plan["first"])
    queue = RunQueue(harness.runner)
    batch = review_all_tasks(queue, plan["first"])
    assert [r.node_id for r in batch.started] == [plan["first"]]
    await finish(harness, queue)


def test_a_bad_mode_is_refused_before_anything_starts(
    project: Project, plan: dict[str, NodeId]
) -> None:
    harness = Harness(project)
    with pytest.raises(RunRefused, match="unknown permission mode"):
        review_all_tasks(RunQueue(harness.runner), plan["feature"], mode="yolo")
    assert harness.store.list_runs() == []


# --- Implement all ---


async def test_implement_all_starts_ready_tasks_that_pass_the_gate(
    project: Project, fake_claude: FakeClaude
) -> None:
    configure(project, "runner:\n  max_concurrent: 1\n")
    feature = add(project, "Search")
    parse = add(project, "Parse", feature)
    write = add(project, "Write", feature)
    after = add(project, "After parse", feature, parse)
    unreviewed = add(project, "Unreviewed", feature)
    locked = add(project, "Locked", feature)
    # Built and pushed, not merged: what depends on it can stack on it.
    pushed = add(project, "Pushed", feature)
    set_status(project, pushed, Status.IN_PROGRESS)
    attach_delivery(project, pushed, branch="apm/pushed")
    stacked = add(project, "Stacked", feature, pushed)
    harness = Harness(project)
    for task in (parse, write, after, locked, stacked):
        harness.review(task)
    take_review_lock(project.apm_dir, locked, "someone-else")

    queue = RunQueue(harness.runner)
    batch = implement_feature(queue, feature)
    assert [r.node_id for r in batch.started] == [parse]
    assert batch.waiting == (write, stacked)
    reasons = {s.node_id: s.reason for s in batch.skipped}
    assert reasons == {
        after: f"blocked: waiting on {handle(parse)}",
        unreviewed: "this task's current content has not been reviewed: run Review task first",
        locked: "under review: wait for its review to finish",
    }
    await finish(harness, queue)
    runs = harness.store.list_runs()
    assert sorted(r.node_id for r in runs) == sorted([parse, write, stacked])
    assert {r.agent for r in runs} == {IMPLEMENTER}


async def test_a_queued_run_that_no_longer_applies_is_dropped_with_why(
    project: Project, fake_claude: FakeClaude
) -> None:
    configure(project, "runner:\n  max_concurrent: 1\n")
    feature = add(project, "Search")
    first, second = add(project, "First", feature), add(project, "Second", feature)
    harness = Harness(project)
    fake_claude.mode("hang")
    queue = RunQueue(harness.runner)
    batch = review_all_tasks(queue, feature)
    assert batch.waiting == (second,)
    delete_node(project, second)
    await harness.runner.cancel(batch.started[0].id)
    pumped = queue.pump()
    assert pumped.started == ()
    ((item, reason),) = [(d.item, d.reason) for d in pumped.dropped]
    assert item == Queued(TASK_REVIEWER, second)
    assert "unknown node" in reason
    assert [r.node_id for r in harness.store.list_runs()] == [first]


# --- the queue itself ---


def test_a_run_waiting_already_is_not_queued_twice(project: Project) -> None:
    queue = RunQueue(Harness(project).runner)
    queue.add([Queued(TASK_REVIEWER, "a"), Queued(TASK_REVIEWER, "a"), Queued(IMPLEMENTER, "a")])
    assert queue.waiting == [Queued(TASK_REVIEWER, "a"), Queued(IMPLEMENTER, "a")]


async def test_runs_asked_for_by_hand_are_not_automatic_ones(
    project: Project, fake_claude: FakeClaude
) -> None:
    (task,) = create_nodes(project, [NewNode(title="Parse")]).created
    harness = Harness(project)
    automation = AutoQueue(harness.runner, debounce_s=0)
    automation.scan()
    automation.pump()
    await finish(harness, automation.manual)
    assert len(harness.store.list_runs()) == 1  # the automatic review of this version
    started = dict(automation.started)
    batch = review_all_tasks(automation.manual, task)
    assert [r.node_id for r in batch.started] == [task]  # it ran automatically: no matter
    await finish(harness, automation.manual)
    assert automation.started == started  # and this one isn't counted as automatic
    assert len(harness.store.list_runs()) == 2


async def test_the_automation_loop_starts_runs_waiting_for_room(
    project: Project, fake_claude: FakeClaude
) -> None:
    configure(project, "runner:\n  max_concurrent: 1\nautomation:\n  task_review: false\n")
    feature = add(project, "Search")
    first, second = add(project, "First", feature), add(project, "Second", feature)
    harness = Harness(project)
    automation = AutoQueue(harness.runner, debounce_s=0)
    batch = review_all_tasks(automation.manual, feature)
    assert batch == Batch(batch.started, (second,), ())
    driver = asyncio.create_task(automation.drive(asyncio.Queue(), tick_s=0.01))
    for _ in range(1000):
        if len(harness.store.list_runs(status="succeeded")) == 2:
            break
        await asyncio.sleep(0.01)
    driver.cancel()
    assert reviewed(harness) == sorted([first, second])
