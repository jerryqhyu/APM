from pathlib import Path

import pytest
from typer.testing import CliRunner

from apm.cli import app
from apm.core.costs import read_costs
from apm.core.delivery import PrRef, feature_branch, parse_pr_url, read_pr
from apm.core.files import read_body
from apm.core.git import git, log
from apm.core.ids import handle
from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.project import Project, load_graph
from apm.core.reconcile import Report, describe, freeze_feature_cost, reconcile
from apm.core.store import Store
from tests.fake_gh import FakeGh


def code_commit(project: Project, name: str, branch: str | None = None) -> str:
    """Makes a commit in the code repo, optionally on a new branch, and returns its SHA."""
    code = project.code_dir
    if branch:
        git(code, ["checkout", "--quiet", "-b", branch])
    (code / name).write_text(name)
    git(code, ["add", name])
    git(code, ["commit", "--quiet", "-m", f"add {name}"])
    return git(code, ["rev-parse", "HEAD"]).strip()


def checkout(project: Project, branch: str) -> None:
    git(project.code_dir, ["checkout", "--quiet", branch])


def head(project: Project) -> str:
    return git(project.code_dir, ["rev-parse", "HEAD"]).strip()


@pytest.fixture
def code(project: Project) -> Project:
    """The project's code directory as a git repo on `main` with one commit."""
    git(project.code_dir, ["init", "--quiet", "-b", "main"])
    code_commit(project, "README.md")
    return project


@pytest.fixture
def gh(fake_gh: FakeGh) -> FakeGh:
    return fake_gh


def delivered(project: Project, title: str, **delivery: object) -> str:
    """A leaf that is in progress, with a delivery attached."""
    (node,) = create_nodes(project, [NewNode(title=title)]).created
    set_status(project, node, Status.IN_PROGRESS)
    attach_delivery(project, node, **delivery)  # ty: ignore[invalid-argument-type]
    return node


def done(report: Report) -> list[tuple[str, str]]:
    return [(r.node_id, r.reason) for r in report.done]


def found(report: Report) -> list[tuple[str, str, str]]:
    return [(f.node_id, f.kind, f.message) for f in report.findings]


# --- commits ---


def test_a_node_whose_commits_are_on_main_becomes_done(code: Project) -> None:
    sha = code_commit(code, "feature.py", branch="apm/feature")
    merged = delivered(code, "Merged", branch="apm/feature", commits=[sha])
    other = code_commit(code, "other.py", branch="apm/other")
    unmerged = delivered(code, "Not merged", branch="apm/other", commits=[other])
    checkout(code, "main")
    git(code.code_dir, ["merge", "--quiet", "--no-ff", "-m", "merge feature", "apm/feature"])

    result = reconcile(code)
    assert done(result) == [(merged, "its commits are on main")]
    assert describe(result)[0].endswith("Merged: done (its commits are on main)")
    graph = load_graph(code)
    assert graph[merged].status is Status.DONE
    assert graph[unmerged].status is Status.IN_PROGRESS
    assert log(code.apm_dir)[0].subject.startswith("status ")
    assert "Reconciled: its commits are on main." in git(code.apm_dir, ["log", "-1", "--format=%b"])
    assert reconcile(code) == Report()  # nothing left to do


def test_a_squash_merge_without_a_pr_is_found_by_its_changes(code: Project) -> None:
    first = code_commit(code, "one.py", branch="apm/squashed")
    second = code_commit(code, "two.py")
    node = delivered(code, "Squashed", branch="apm/squashed", commits=[first, second])
    checkout(code, "main")
    git(code.code_dir, ["merge", "--quiet", "--squash", "apm/squashed"])
    git(code.code_dir, ["commit", "--quiet", "-m", "Squashed (#7)"])
    assert done(reconcile(code)) == [(node, "its changes are on main (squashed or rebased)")]


def test_a_rebased_delivery_without_a_pr_is_found_by_its_changes(code: Project) -> None:
    sha = code_commit(code, "rebased.py", branch="apm/rebased")
    node = delivered(code, "Rebased", branch="apm/rebased", commits=[sha])
    checkout(code, "main")
    code_commit(code, "meanwhile.py")  # main moved on, so a cherry-pick gets a new SHA
    git(code.code_dir, ["cherry-pick", sha])
    assert head(code) != sha
    assert done(reconcile(code)) == [(node, "its changes are on main (squashed or rebased)")]


def test_different_changes_with_the_same_files_do_not_count(code: Project) -> None:
    sha = code_commit(code, "feature.py", branch="apm/feature")
    node = delivered(code, "Not merged", branch="apm/feature", commits=[sha])
    checkout(code, "main")
    (code.code_dir / "feature.py").write_text("something else")
    git(code.code_dir, ["add", "feature.py"])
    git(code.code_dir, ["commit", "--quiet", "-m", "other work"])
    assert reconcile(code) == Report()
    assert load_graph(code)[node].status is Status.IN_PROGRESS


# --- pull requests ---


def test_a_pr_merged_into_main_makes_the_node_done(code: Project, gh: FakeGh) -> None:
    merged = delivered(code, "Merged", branch="apm/a", pr=gh.add(20, merged=True, state="closed"))
    still_open = delivered(code, "Open", branch="apm/b", pr=gh.add(21))
    assert done(reconcile(code)) == [(merged, "PR #20 merged into main")]
    assert load_graph(code)[still_open].status is Status.IN_PROGRESS
    assert ["api", "repos/o/r/pulls/20"] in gh.calls  # REST, not `gh pr view`
    assert not any(call[0] == "pr" for call in gh.calls)


def test_a_pr_merged_into_a_stack_branch_is_not_done_and_says_so_once(
    code: Project, gh: FakeGh
) -> None:
    code_commit(code, "base.py", branch="apm/base")
    base = delivered(code, "Base", branch="apm/base", pr=gh.add(20, base="main"))
    sha = code_commit(code, "top.py", branch="apm/top")
    checkout(code, "apm/base")
    git(code.code_dir, ["merge", "--quiet", "--no-ff", "-m", "merge top", "apm/top"])
    url = gh.add(21, base="apm/base", state="closed", merged=True, merge_commit=head(code))
    top = delivered(code, "Top", branch="apm/top", commits=[sha], pr=url)
    checkout(code, "main")

    message = "PR #21 merged into apm/base (#20's branch), not yet on main"
    assert found(reconcile(code)) == [(top, "merged-elsewhere", message)]
    assert load_graph(code)[top].status is Status.IN_PROGRESS
    assert load_graph(code)[base].status is Status.IN_PROGRESS
    body = read_body(code.apm_dir, top)
    assert f"**Reconcile:** {message}." in body
    commits = len(log(code.apm_dir))

    assert found(reconcile(code)) == [(top, "merged-elsewhere", message)]  # still reported
    assert read_body(code.apm_dir, top) == body  # but the note isn't written twice
    assert len(log(code.apm_dir)) == commits


def test_a_stack_branch_that_lands_on_main_brings_its_merged_pr_with_it(
    code: Project, gh: FakeGh
) -> None:
    code_commit(code, "base.py", branch="apm/base")
    code_commit(code, "top.py", branch="apm/top")
    checkout(code, "apm/base")
    git(code.code_dir, ["merge", "--quiet", "--no-ff", "-m", "merge top", "apm/top"])
    merge = head(code)
    url = gh.add(21, base="apm/base", state="closed", merged=True, merge_commit=merge)
    top = delivered(code, "Top", branch="apm/top", pr=url)
    checkout(code, "main")
    git(code.code_dir, ["merge", "--quiet", "--no-ff", "-m", "merge base", "apm/base"])
    assert done(reconcile(code)) == [(top, "PR #21 merged into apm/base, which is now on main")]


def test_a_pr_closed_without_merging_is_reported(code: Project, gh: FakeGh) -> None:
    node = delivered(code, "Abandoned", branch="apm/x", pr=gh.add(30, state="closed"))
    assert found(reconcile(code)) == [(node, "closed", "PR #30 was closed without merging")]
    assert load_graph(code)[node].status is Status.IN_PROGRESS


# --- retargeting ---


def stacked_pair(code: Project, gh: FakeGh) -> tuple[str, str]:
    """#20 (apm/base → main) is merged and done; #21 (apm/top) still targets apm/base."""
    code_commit(code, "base.py", branch="apm/base")
    base = delivered(code, "Base", branch="apm/base", pr=gh.add(20, merged=True, state="closed"))
    code_commit(code, "top.py", branch="apm/top")
    top = delivered(code, "Top", branch="apm/top", pr=gh.add(21, base="apm/base"))
    checkout(code, "main")
    return base, top


def test_an_open_pr_on_a_landed_branch_is_reported_for_retargeting(
    code: Project, gh: FakeGh
) -> None:
    base, top = stacked_pair(code, gh)
    result = reconcile(code)
    assert done(result) == [(base, "PR #20 merged into main")]
    assert found(result) == [
        (
            top,
            "retarget",
            "PR #21 still targets apm/base (#20's branch), which has landed: retarget it to main",
        )
    ]
    assert gh.pr(21)["base"]["ref"] == "apm/base"  # only reported
    assert not any("PATCH" in call for call in gh.calls)


def test_retarget_points_the_pr_at_the_default_branch(code: Project, gh: FakeGh) -> None:
    _, top = stacked_pair(code, gh)
    result = reconcile(code, retarget=True)
    assert found(result) == [
        (top, "retargeted", "PR #21 retargeted from apm/base (#20's branch) to main")
    ]
    assert ["api", "-X", "PATCH", "repos/o/r/pulls/21", "-f", "base=main"] in gh.calls
    assert gh.pr(21)["base"]["ref"] == "main"
    assert found(reconcile(code)) == []  # nothing left to retarget


def test_an_open_pr_on_a_branch_that_has_not_landed_is_left_alone(
    code: Project, gh: FakeGh
) -> None:
    code_commit(code, "base.py", branch="apm/base")
    delivered(code, "Base", branch="apm/base", pr=gh.add(20))
    code_commit(code, "top.py", branch="apm/top")
    delivered(code, "Top", branch="apm/top", pr=gh.add(21, base="apm/base"))
    checkout(code, "main")
    assert reconcile(code, retarget=True) == Report()


# --- reading pull requests ---


def test_pr_urls_parse_into_rest_paths() -> None:
    ref = parse_pr_url("https://github.com/jerryqhyu/APM/pull/21")
    assert ref == PrRef("github.com", "jerryqhyu", "APM", 21)
    assert ref.api_path == "repos/jerryqhyu/APM/pulls/21"
    assert ref.host_args == []
    enterprise = parse_pr_url("https://git.example.com/team/app/pull/3/files")
    assert enterprise is not None
    assert enterprise.host_args == ["--hostname", "git.example.com"]
    assert parse_pr_url("https://example.com/merge_requests/3") is None


def test_read_pr_reads_state_base_and_merge_commit(code: Project, gh: FakeGh) -> None:
    url = gh.add(5, base="apm/base", state="closed", merged=True, merge_commit="abc123")
    pr = read_pr(code.code_dir, url)
    assert pr is not None
    assert (pr.number, pr.state, pr.merged, pr.base, pr.merge_commit) == (
        5,
        "closed",
        True,
        "apm/base",
        "abc123",
    )
    assert read_pr(code.code_dir, "https://github.com/o/r/pull/404") is None  # gh says Not Found
    assert read_pr(code.code_dir, "not a url") is None


def test_nodes_that_cannot_be_judged_are_left_alone(code: Project, gh: FakeGh) -> None:
    unknown_pr = delivered(code, "Unknown PR", pr="https://github.com/o/r/pull/9")
    no_commits = delivered(code, "Branch only", branch="apm/x")
    missing = delivered(code, "Commit not in this clone", commits=["abcdef1234567"])
    (todo,) = create_nodes(code, [NewNode(title="Not started")]).created
    assert reconcile(code) == Report()
    graph = load_graph(code)
    assert {graph[n].status for n in (unknown_pr, no_commits, missing)} == {Status.IN_PROGRESS}
    assert graph[todo].status is Status.TODO


def test_without_gh_a_pr_node_falls_back_to_its_commits(
    code: Project, gh: FakeGh, tmp_path: Path
) -> None:
    gh.missing(tmp_path)
    sha = code_commit(code, "feature.py", branch="apm/feature")
    node = delivered(code, "Merged", commits=[sha], pr="https://github.com/o/r/pull/1")
    checkout(code, "main")
    git(code.code_dir, ["merge", "--quiet", "--no-ff", "-m", "merge", "apm/feature"])
    assert done(reconcile(code)) == [(node, "its commits are on main")]


def test_the_cli_reports_and_with_retarget_retargets(
    code: Project, gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, top = stacked_pair(code, gh)
    monkeypatch.chdir(code.code_dir)
    runner = CliRunner()
    first = runner.invoke(app, ["reconcile"])
    assert first.exit_code == 0, first.output
    assert "PR #20 merged into main" in first.output
    assert "retarget it to main" in first.output
    assert gh.pr(21)["base"]["ref"] == "apm/base"

    second = runner.invoke(app, ["reconcile", "--retarget"])
    assert second.exit_code == 0, second.output
    assert f"{handle(top)} Top: PR #21 retargeted from apm/base (#20's branch) to main" in (
        second.output
    )
    assert ["api", "-X", "PATCH", "repos/o/r/pulls/21", "-f", "base=main"] in gh.calls


# --- freezing a task's cost ---


def test_a_merged_pr_freezes_the_tasks_cost_once(code: Project, gh: FakeGh) -> None:
    store = Store(code.apm_dir)

    def run(node: str, agent: str, cost: float) -> None:
        run_id = store.create_run(node_id=node, agent=agent).id
        store.finish_run(run_id, "succeeded", cost_usd=cost)

    url = gh.add(20, merged=True, state="closed")
    node = delivered(code, "Merged", branch="apm/a", pr=url)
    run(node, "apm-implementer", 1.5)
    run(node, "apm-task-reviewer", 0.25)
    reconcile(code)
    (record,) = read_costs(code.apm_dir).values()
    assert (record.node_id, record.merged) == (node, url)
    assert record.total == 1.75
    assert record.by_agent == {"apm-implementer": 1.5, "apm-task-reviewer": 0.25}

    run(node, "apm-implementer", 3.0)  # a run after the merge
    reconcile(code)
    set_status(code, node, Status.IN_PROGRESS)  # so reconcile judges it again
    assert done(reconcile(code)) == [(node, "PR #20 merged into main")]
    assert read_costs(code.apm_dir) == {node: record}


def test_a_node_done_without_a_merged_pr_freezes_nothing(code: Project) -> None:
    sha = code_commit(code, "feature.py")
    delivered(code, "On main", branch="main", commits=[sha])
    assert len(reconcile(code).done) == 1
    assert read_costs(code.apm_dir) == {}


# --- frozen feature costs ---


def feature_with_review(project: Project, title: str, pr: str) -> tuple[str, str]:
    """A feature with one done task that cost 1.5, and an in-progress review step delivered by
    `pr`. Returns the feature and its branch.
    """
    feature, task = create_nodes(
        project, [NewNode(title=title, key="f"), NewNode(title="Task", parent_key="f")]
    ).created
    graph = load_graph(project)
    review = graph.review_step(feature)
    assert review is not None
    set_status(project, task, Status.DONE)
    set_status(project, review, Status.IN_PROGRESS)
    branch = feature_branch(graph, feature)
    attach_delivery(project, review, branch=branch, pr=pr)
    store = Store(project.apm_dir)
    run = store.create_run(node_id=task, agent="apm-implementer")
    store.finish_run(run.id, "succeeded", cost_usd=1.5)
    return feature, branch


def test_a_feature_whose_branch_merged_into_main_has_its_cost_frozen(
    code: Project, gh: FakeGh
) -> None:
    merged, branch = feature_with_review(code, "Merged", gh.add(30, merged=True, state="closed"))
    still_open, _ = feature_with_review(code, "Open", gh.add(31))
    reconcile(code)
    costs = read_costs(code.apm_dir)
    assert set(costs) == {merged}
    assert still_open not in costs
    record = costs[merged]
    assert (record.merged, record.total, record.by_agent) == (
        branch,
        1.5,
        {"apm-implementer": 1.5},
    )

    run = Store(code.apm_dir).create_run(node_id=merged, agent="apm-feature-reviewer")
    Store(code.apm_dir).finish_run(run.id, "succeeded", cost_usd=2.0)
    freeze_feature_cost(code, merged)
    assert read_costs(code.apm_dir) == costs
