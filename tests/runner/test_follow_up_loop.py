"""The feature-review loop, end to end with a fake `claude`, real git and a fake `gh`: a review
finds gaps, its follow-ups are accepted, reviewed, built and merged on their own, the review runs
again and passes, and the feature's pull request into main is opened.
"""

from typing import Any

import pytest

from apm.core.delivery import choose_base
from apm.core.git import git
from apm.core.model import NodeId, Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.project import Project, load_graph
from apm.core.report import REPORTER
from apm.runner.auto import IMPLEMENTER, TASK_REVIEWER, AutoQueue
from apm.runner.runner import FEATURE_REVIEWER, Runner
from tests.conftest import CodeRepo, configure
from tests.core.test_feature_review import built
from tests.fake_gh import FakeGh
from tests.runner.fake import FakeClaude
from tests.runner.test_auto import feature, recording
from tests.runner.test_runner import Harness


def emulate_implementer(code_repo: CodeRepo, runner: Runner) -> None:
    """The fake `claude` writes no code, so do what the implementer would: push a branch from the
    base APM chose, record it, and mark the node in progress. APM then opens its pull request.
    """
    real_start = runner.start

    def start(agent: str, node_id: NodeId, *args: Any, **kwargs: Any) -> Any:
        run = real_start(agent, node_id, *args, **kwargs)
        if agent == IMPLEMENTER:
            project = code_repo.project
            base = choose_base(project, node_id).branch
            branch = f"apm/{node_id[-6:]}"
            sha = code_repo.push_branch(branch, base=f"origin/{base}")
            attach_delivery(project, node_id, branch=branch, commits=[sha])
            set_status(project, node_id, Status.IN_PROGRESS)
        return run

    runner.start = start  # ty: ignore[invalid-assignment]


async def test_gaps_are_followed_up_built_merged_and_reviewed_again(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    feature_node, step, _, branch = built(code_repo, project, "Write")
    harness = Harness(project)
    emulate_implementer(code_repo, harness.runner)
    queue = AutoQueue(harness.runner, debounce_s=0)

    async def turn() -> list[tuple[str, NodeId]]:
        before = {r.id for r in harness.store.list_runs()}
        queue.scan()
        queue.pump()
        await harness.settle()
        runs = [r for r in harness.store.list_runs() if r.id not in before]
        assert {r.status for r in runs} <= {"succeeded"}
        return sorted((r.agent, r.node_id) for r in runs)

    fake_claude.mode("review-stacked-gaps")
    assert await turn() == [(FEATURE_REVIEWER, step)]
    fix, report = sorted(harness.store.follow_ups(), key=lambda n: load_graph(project)[n].title)
    graph = load_graph(project)
    assert (graph[fix].title, graph[report].title) == ("Handle errors", "Report errors")
    assert graph.dependencies(report) == (fix,)
    assert graph[step].status is Status.TODO
    assert harness.store.list_proposals(status="pending") == []

    fake_claude.mode("")
    assert await turn() == sorted([(TASK_REVIEWER, fix), (TASK_REVIEWER, report)])
    assert await turn() == [(IMPLEMENTER, fix)]
    assert await turn() == [(IMPLEMENTER, report)]  # stacked on fix's unmerged branch
    assert await turn() == []
    prs = [c for c in fake_gh.calls if c[:3] == ["api", "-X", "POST"]]
    assert [fake_gh.field(c, "base") for c in prs] == [branch, branch]  # one PR each
    assert [fake_gh.field(c, "head") for c in prs] == [f"apm/{fix[-6:]}", f"apm/{report[-6:]}"]

    # Both merge into the feature branch: reconcile finishes them and the review runs again.
    tip = code_repo.push_branch("merged", base=f"origin/apm/{report[-6:]}")
    git(code_repo.dir, ["push", "--quiet", "origin", f"{tip}:refs/heads/{branch}"])
    queue.next_reconcile = 0
    assert queue.reconcile()
    graph = load_graph(project)
    assert (graph[fix].status, graph[report].status) == (Status.DONE, Status.DONE)
    # It passes: the feature's pull request opens, and its report is written onto the branch.
    assert await turn() == sorted([(FEATURE_REVIEWER, step), (REPORTER, feature_node)])
    prs = [c for c in fake_gh.calls if c[:3] == ["api", "-X", "POST"]]
    assert (fake_gh.field(prs[-1], "base"), fake_gh.field(prs[-1], "head")) == ("main", branch)
    assert load_graph(project)[step].status is Status.IN_PROGRESS
    written = harness.store.latest_report(feature_node)
    assert written is not None
    assert written.sha == git(code_repo.dir, ["rev-parse", f"origin/{branch}"]).strip()
    # The record had the whole story: both reviews, and the follow-ups that closed the gaps.
    prompt = fake_claude.calls[-1]["prompt"]
    assert "Incomplete" in prompt
    assert "“Handle errors” (done" in prompt
    assert await turn() == []
    assert load_graph(project)[feature_node].status is not Status.DONE  # until its PR merges


# --- each switch stops only its own step ---


def passing_follow_up(project: Project, harness: Harness) -> tuple[NodeId, NodeId]:
    """A follow-up and an ordinary task, both with a passing task review."""
    top, _, _ = feature(project, "Parse")
    other, fix = create_nodes(
        project, [NewNode(title="Other", parent=top), NewNode(title="Fix", parent=top)]
    ).created
    harness.store.add_follow_ups([fix], "proposal")
    harness.review(other)
    harness.review(fix)
    return other, fix


def test_only_a_passing_follow_up_is_queued_for_the_implementer(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(project)
    other, fix = passing_follow_up(project, harness)
    parent = load_graph(project)[fix].parent
    (unreviewed,) = create_nodes(project, [NewNode(title="Later", parent=parent)]).created
    harness.store.add_follow_ups([unreviewed], "proposal")
    started = recording(harness, monkeypatch)
    queue = AutoQueue(harness.runner, debounce_s=0)
    queue.scan()
    queue.pump()
    assert (IMPLEMENTER, fix) in started
    assert (IMPLEMENTER, other) not in started  # passing, but not a follow-up
    assert (IMPLEMENTER, unreviewed) not in started  # a follow-up with no passing review
    assert (TASK_REVIEWER, unreviewed) in started


def test_implement_follow_ups_off_stops_only_the_implementer(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(project, "automation:\n  implement_follow_ups: false\n")
    harness = Harness(project)
    _, fix = passing_follow_up(project, harness)
    parent = load_graph(project)[fix].parent
    (fresh,) = create_nodes(project, [NewNode(title="Fresh", parent=parent)]).created
    started = recording(harness, monkeypatch)
    queue = AutoQueue(harness.runner, debounce_s=0)
    queue.scan()
    queue.pump()
    assert (IMPLEMENTER, fix) not in started
    assert (TASK_REVIEWER, fresh) in started  # task reviews go on


async def test_accept_follow_ups_off_leaves_the_proposal_and_the_rest_runs(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    configure(project, "automation:\n  accept_follow_ups: false\n")
    _, step, _, _ = built(code_repo, project, "Write")
    harness = Harness(project)
    fake_claude.mode("review-gaps")
    queue = AutoQueue(harness.runner, debounce_s=0)
    queue.scan()
    queue.pump()
    await harness.settle()
    assert [(r.agent, r.node_id) for r in harness.store.list_runs()] == [(FEATURE_REVIEWER, step)]
    assert len(harness.store.list_proposals(status="pending")) == 1  # waits for you
    assert harness.store.follow_ups() == set()
    assert load_graph(project)[step].status is Status.TODO  # the verdict still moved the step
