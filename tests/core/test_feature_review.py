"""The plain code around the feature reviewer: what it reviews, where its verdict goes, and what
a verdict does to the review step.
"""

from datetime import UTC, datetime

import pytest

from apm.core.delivery import choose_base
from apm.core.feature_review import (
    FeatureReview,
    ReviewRefused,
    check_reviewable,
    conclude,
    find_target,
    parse_review,
    prepare_worktree,
    record_review,
    render_section,
    replace_section,
)
from apm.core.files import read_body
from apm.core.git import git
from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, decompose, edit_body, set_status
from apm.core.project import Project, load_graph
from apm.core.reconcile import reconcile
from apm.core.store import Store
from tests.conftest import CodeRepo
from tests.fake_gh import FakeGh

COMPLETE = {
    "verdict": "complete",
    "summary": "All there.",
    "criteria": [{"criterion": "It exports", "status": "met", "evidence": "export.py:10"}],
    "issues": [],
    "follow_ups": [],
}
GAPS = {
    "verdict": "incomplete",
    "summary": "Import is missing.",
    "criteria": [
        {"criterion": "It exports", "status": "met", "evidence": "export.py:10"},
        {"criterion": "It imports | round trips", "status": "missing", "evidence": ""},
    ],
    "issues": [
        {"severity": "major", "problem": "No import", "where": "cli.py", "suggestion": "Add it"}
    ],
    "follow_ups": [
        {
            "key": "i",
            "title": "Import",
            "kind": "work",
            "intent": "Read it back.",
            "acceptance": ["round trip"],
            "depends_on_keys": [],
            "depends_on_ids": [],
        }
    ],
}


def feature(project: Project, *titles: str) -> tuple[str, str, list[str]]:
    node = create_nodes(project, [NewNode(title="Export")]).created[0]
    tasks = list(decompose(project, node, [NewNode(title=t) for t in titles]).created)
    step = load_graph(project).review_step(node)
    assert step is not None
    return node, step, tasks


def built(repo: CodeRepo, project: Project, *titles: str) -> tuple[str, str, list[str], str]:
    """A feature whose tasks are done and merged into its branch. Returns the branch too."""
    node, step, tasks = feature(project, *titles)
    branch = choose_base(project, tasks[0]).branch
    assert branch is not None
    for task in tasks:
        sha = repo.push_branch(f"apm/{task[-6:]}", base=f"origin/{branch}")
        git(repo.dir, ["push", "--quiet", "origin", f"{sha}:refs/heads/{branch}"])
        git(repo.dir, ["fetch", "--quiet", "origin"])
        attach_delivery(project, task, branch=f"apm/{task[-6:]}", commits=[sha])
        set_status(project, task, Status.DONE)
    return node, step, tasks, branch


# --- what is reviewed ---


def test_a_review_waits_until_every_task_is_done(project: Project) -> None:
    node, step, (first, second) = feature(project, "First", "Second")
    set_status(project, first, Status.DONE)
    with pytest.raises(ReviewRefused, match="waits until every task is done"):
        check_reviewable(load_graph(project), step, None)
    set_status(project, second, Status.DROPPED)  # dropped counts as finished
    with pytest.raises(ReviewRefused, match="nothing under this node has been delivered"):
        check_reviewable(load_graph(project), node, None)  # started on the feature: same step
    check_reviewable(load_graph(project), step, "some-branch")  # named: fine

    set_status(project, step, Status.DROPPED)
    with pytest.raises(ReviewRefused, match="review step is dropped"):
        check_reviewable(load_graph(project), step, "some-branch")


def test_it_reviews_the_feature_branch(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, step, tasks, branch = built(code_repo, project, "Write", "Read")
    check_reviewable(load_graph(project), node, None)

    target = find_target(project, node)  # started on the feature, or on its step
    assert (target.branch, target.why) == (branch, "it is the feature branch")
    assert target.sha == git(code_repo.dir, ["rev-parse", f"origin/{branch}"]).strip()
    assert (target.feature, target.step, target.default) == (node, step, "main")
    assert [(t.node_id, t.included) for t in target.tasks] == [(t, True) for t in tasks]
    assert find_target(project, step) == target


def test_a_named_branch_wins_and_must_exist(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, _, _, _ = built(code_repo, project, "Write")
    other = code_repo.push_branch("experiment")
    target = find_target(project, node, "origin/experiment")
    assert (target.branch, target.sha, target.why) == ("experiment", other, "you named it")
    with pytest.raises(ReviewRefused, match="no branch 'nope'"):
        find_target(project, node, "nope")


def test_without_a_review_step_the_branch_is_worked_out(
    code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    # Plans from before review steps: a leaf delivered on its own.
    project = code_repo.project
    (leaf,) = create_nodes(project, [NewNode(title="Solo")]).created
    with pytest.raises(ReviewRefused, match="nothing under this node has been delivered"):
        find_target(project, leaf)

    sha = code_repo.push_branch("apm/solo")
    attach_delivery(project, leaf, branch="apm/solo")
    target = find_target(project, leaf)
    assert (target.branch, target.sha, target.step) == ("apm/solo", sha, None)
    assert target.why == "it holds the tasks' unmerged work"

    code_repo.merge("apm/solo")
    assert find_target(project, leaf).branch == "main"

    url = fake_gh.add(4, base="feature/x", state="closed", merged=True)
    code_repo.push_branch("feature/x")
    attach_delivery(project, leaf, pr=url)
    assert find_target(project, leaf).why == "the tasks' pull requests merged into it"


def test_the_checkout_is_the_reviewed_commit_and_is_replaced_next_time(
    code_repo: CodeRepo,
) -> None:
    project = code_repo.project
    node, _, _, _ = built(code_repo, project, "Write")
    target = find_target(project, node)
    path = prepare_worktree(project, node, target)
    assert git(path, ["rev-parse", "HEAD"]).strip() == target.sha
    assert ".claude/worktrees/apm-review-" in str(path)
    (path / "scratch.txt").write_text("left over")
    assert prepare_worktree(project, node, target) == path
    assert not (path / "scratch.txt").exists()


# --- the verdict ---


def test_the_verdict_is_held_to_its_evidence() -> None:
    assert parse_review(COMPLETE).complete
    assert not parse_review(GAPS).complete
    overclaimed = parse_review({**GAPS, "verdict": "complete"})
    assert not overclaimed.complete
    section = render_section(overclaimed, branch="b", sha="0" * 40)
    assert "**Incomplete**" in section
    assert "The reviewer said complete" in section
    with pytest.raises(ReviewRefused, match="not a valid feature review"):
        parse_review({"verdict": "maybe"})


def test_the_section_is_a_table_and_replaces_only_itself() -> None:
    review = FeatureReview.model_validate(GAPS)
    when = datetime(2026, 11, 21, tzinfo=UTC)
    section = render_section(review, branch="feature/x", sha="abcdef1234", when=when)
    assert section.startswith("## Feature review\n\n**Incomplete** · `feature/x` at `abcdef1`")
    assert "| Missing | It imports \\| round trips | — |" in section
    assert "- **major**: No import (cli.py) Suggestion: Add it" in section
    assert "1 follow-up task proposed" in section

    body = "## Intent\n\nShip it.\n\n## Feature review\n\nold\n\n## Notes\n\nKeep me.\n"
    updated = replace_section(body, "Feature review", "## Feature review\n\nnew\n")
    assert updated == "## Intent\n\nShip it.\n\n## Feature review\n\nnew\n\n## Notes\n\nKeep me.\n"
    assert replace_section("## Intent\n\nx\n", "Feature review", "## Feature review\n\ny\n") == (
        "## Intent\n\nx\n\n## Feature review\n\ny\n"
    )


def test_recording_writes_the_step_and_proposes_follow_ups_on_the_feature(
    project: Project,
) -> None:
    node, step, _ = feature(project, "Write")
    edit_body(project, node, "## Intent\n\nThe spec.\n")
    store = Store(project.apm_dir)
    review = parse_review(GAPS)

    first = record_review(
        project, store, step, review, branch="feature/x", sha="a" * 40, run="r1", feature=node
    )
    assert not first.complete
    assert first.proposal is not None
    assert first.proposal.node_id == node  # accepting it adds tasks to the feature
    assert [c["title"] for c in first.proposal.data["children"]] == ["Import"]
    assert first.proposal.data["warnings"] == ["**major**: No import (cli.py) Suggestion: Add it"]
    assert "## Feature review" in read_body(project.apm_dir, step)
    assert read_body(project.apm_dir, node) == "## Intent\n\nThe spec.\n"  # the spec is untouched

    # A later turn of the same review replaces what the first one proposed.
    second = record_review(
        project,
        store,
        step,
        review,
        branch="feature/x",
        sha="b" * 40,
        run="r2",
        supersedes=["r1"],
        feature=node,
    )
    assert second.replaced == (first.proposal.id,)
    assert store.get_proposal(first.proposal.id).status == "rejected"
    assert read_body(project.apm_dir, step).count("## Feature review") == 1
    assert "`bbbbbbb`" in read_body(project.apm_dir, step)


def test_gaps_send_the_step_back_to_do(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, step, _, _ = built(code_repo, project, "Write")
    set_status(project, step, Status.IN_PROGRESS)
    target = find_target(project, node)
    concluded = conclude(project, target, parse_review(GAPS))
    assert concluded.status is Status.TODO
    assert concluded.pr is None
    assert load_graph(project)[step].status is Status.TODO


def test_a_pass_opens_the_feature_s_pull_request(code_repo: CodeRepo, fake_gh: FakeGh) -> None:
    project = code_repo.project
    node, step, _, branch = built(code_repo, project, "Write")
    edit_body(project, node, "## Intent\n\nThe spec.\n")
    target = find_target(project, node)
    record_review(
        project,
        Store(project.apm_dir),
        step,
        parse_review(COMPLETE),
        branch=branch,
        sha=target.sha,
        feature=node,
    )
    concluded = conclude(project, target, parse_review(COMPLETE))

    assert concluded.status is Status.IN_PROGRESS
    assert concluded.pr is not None
    assert (concluded.pr.action, concluded.pr.base) == ("opened", "main")
    post = next(call for call in fake_gh.calls if call[:3] == ["api", "-X", "POST"])
    assert f"head={branch}" in post
    assert "title=Export" in post  # the feature's title, not the step's
    body = next(arg for arg in post if arg.startswith("body="))
    assert "The spec." in body
    assert "## Feature review" in body
    graph = load_graph(project)
    assert graph[step].status is Status.IN_PROGRESS
    delivery = graph[step].delivery
    assert delivery is not None
    assert delivery.pr == "https://github.com/o/r/pull/7"
    assert delivery.commits == (target.sha,)  # what was reviewed, for reconcile without a PR


def test_a_pass_on_the_destination_itself_finishes_the_feature(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, step, _, _ = built(code_repo, project, "Write")
    target = find_target(project, node, "main")  # e.g. the work already went straight to main
    concluded = conclude(project, target, parse_review(COMPLETE))
    assert concluded.landed
    graph = load_graph(project)
    assert graph[step].status is Status.DONE
    assert graph[node].status is Status.DONE


def test_without_a_pull_request_reconcile_sees_the_reviewed_commit_land(
    code_repo: CodeRepo, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GitHub refuses (or there is none): the pass can't open the pull request, but the reviewed
    # commit is recorded, so the feature is done once that commit reaches main, however it got
    # there.
    monkeypatch.setenv("FAKE_GH", "fail")
    project = code_repo.project
    node, step, _, branch = built(code_repo, project, "Write")
    target = find_target(project, node)
    concluded = conclude(project, target, parse_review(COMPLETE))
    assert concluded.pr is not None
    assert concluded.pr.problem is not None
    assert reconcile(project).done == []

    git(code_repo.dir, ["fetch", "--quiet", "origin"])
    code_repo.merge(f"origin/{branch}")
    report = reconcile(project)
    assert [r.node_id for r in report.done] == [step, node]
    assert load_graph(project)[node].status is Status.DONE
