"""The feature reviewer, end to end through the runner, with a fake `claude` and a fake `gh`."""

import pytest

from apm.core.feature_review import ReviewRefused
from apm.core.files import read_body
from apm.core.model import Status
from apm.core.ops import NewNode, create_nodes, decompose, set_status
from apm.core.project import load_graph
from apm.runner.agents import RunRefused
from tests.conftest import CodeRepo
from tests.core.test_feature_review import accept_follow_ups, built
from tests.fake_gh import FakeGh
from tests.runner.fake import FakeClaude
from tests.runner.test_runner import Harness, option


def notices(harness: Harness, run_id: str) -> list[tuple[str, str]]:
    return [
        (e.data["level"], e.data["text"])
        for e in harness.store.list_events(run_id)
        if e.data["type"] == "apm_notice"
    ]


async def test_a_passing_review_opens_the_feature_s_pull_request(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    feature, step, _tasks, branch = built(code_repo, project, "Write", "Read")
    harness = Harness(project)

    run = harness.runner.start("apm-feature-reviewer", feature)  # on the feature: its step runs
    assert run.node_id == step
    await harness.runner.wait(run.id)
    assert harness.store.get_run(run.id).status == "succeeded"

    (call,) = fake_claude.calls
    assert call["agent"] == "apm-feature-reviewer"
    assert ".claude/worktrees/apm-review-" in call["cwd"]  # a checkout of the reviewed commit
    assert f"as built on branch {branch} at commit" in call["prompt"]
    assert "it is the feature branch" in call["prompt"]
    assert "in this commit" in call["prompt"]
    assert "--json-schema" in call["argv"]
    assert "Write" in option(call, "--disallowedTools")
    assert "Bash(git diff *)" in option(call, "--allowedTools").split(",")

    said = notices(harness, run.id)
    assert said[0][1].startswith(f"Reviewing `{branch}` at ")
    assert said[-1][0] == "info"
    assert "**Complete**" in said[-1][1]
    assert "Its pull request into main: https://github.com/o/r/pull/7" in said[-1][1]

    graph = load_graph(project)
    assert graph[step].status is Status.IN_PROGRESS
    delivery = graph[step].delivery
    assert delivery is not None
    assert delivery.pr == "https://github.com/o/r/pull/7"
    assert "**Complete**" in read_body(project.apm_dir, step)
    assert harness.store.list_proposals() == []


async def test_gaps_become_follow_up_tasks_and_a_reply_replaces_them(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    accept_follow_ups(project, on=False)  # proposals wait for you
    feature, step, _tasks, _ = built(code_repo, project, "Write")
    fake_claude.mode("review-gaps")
    harness = Harness(project)

    run = harness.runner.start("apm-feature-reviewer", step)
    await harness.runner.wait(run.id)
    (first,) = harness.store.list_proposals(status="pending")
    assert first.node_id == feature
    assert [c["title"] for c in first.data["children"]] == ["Handle errors"]
    level, text = notices(harness, run.id)[-1]
    assert level == "warning"
    assert "**Incomplete**: 1 of 2 criteria met" in text
    assert "1 follow-up task waits under Proposals" in text
    assert load_graph(project)[step].status is Status.TODO
    assert "proposal.created" in harness.types()

    reply = harness.runner.continue_run(run.id, "Look again at error handling.")
    await harness.runner.wait(reply.id)
    assert harness.store.get_run(reply.id).status == "succeeded"
    assert fake_claude.calls[-1]["cwd"] == fake_claude.calls[0]["cwd"]  # same checkout
    (second,) = harness.store.list_proposals(status="pending")
    assert second.id != first.id
    assert harness.store.get_proposal(first.id).status == "rejected"


async def test_reviews_wait_for_the_tasks_and_review_steps_are_not_built(
    code_repo: CodeRepo, fake_claude: FakeClaude
) -> None:
    project = code_repo.project
    feature = create_nodes(project, [NewNode(title="Feature")]).created[0]
    (task,) = decompose(project, feature, [NewNode(title="Task")]).created
    step = load_graph(project).review_step(feature)
    assert step is not None
    harness = Harness(project)

    with pytest.raises(ReviewRefused, match="waits until every task is done"):
        harness.runner.start("apm-feature-reviewer", feature)
    with pytest.raises(RunRefused, match="run Review feature on it"):
        harness.runner.start("apm-implementer", step)
    with pytest.raises(RunRefused, match="run Review feature on it"):
        harness.runner.start("apm-decomposer", step)

    set_status(project, task, Status.DONE)
    with pytest.raises(ReviewRefused, match="nothing under this node has been delivered"):
        harness.runner.start("apm-feature-reviewer", feature)  # no branch to review: said at once

    # A named branch that doesn't exist fails the run, with the reason, before Claude starts.
    run = harness.runner.start("apm-feature-reviewer", feature, branch="nope")
    await harness.runner.wait(run.id)
    failed = harness.store.get_run(run.id)
    assert failed.status == "failed"
    assert failed.error == "there is no branch 'nope' here or on origin"
    assert fake_claude.calls == []
