"""Where a feature report goes: the feature branch, the path, the commit APM makes and pushes,
and the link in the feature's pull request. Real git, a local `origin`, and a fake `gh`.
"""

import pytest

from apm.core import report_branch
from apm.core.delivery import choose_base
from apm.core.git import GitError, git, log
from apm.core.ids import handle
from apm.core.model import Status
from apm.core.ops import (
    NewNode,
    attach_delivery,
    create_nodes,
    decompose,
    set_status,
    update_node,
)
from apm.core.project import load_graph
from apm.core.report import ReportAnswer, ReportRefused
from apm.core.report_branch import (
    check_reportable,
    commit_report,
    find_target,
    link_report,
    prepare_worktree,
    report_subject,
    report_url,
    save_report,
    with_link,
)
from apm.core.store import Store
from tests.conftest import CodeRepo
from tests.core.test_feature_review import built, feature
from tests.fake_gh import FakeGh


def tip(repo: CodeRepo, branch: str) -> str:
    return git(repo.dir, ["rev-parse", f"origin/{branch}"]).strip()


def test_only_a_feature_with_work_has_a_report(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, step, (task,) = feature(project, "Write")
    graph = load_graph(project)
    assert report_subject(graph, step) == node  # a review step's report is its feature's
    assert report_subject(graph, node) == node
    with pytest.raises(ReportRefused, match="only a feature has a report"):
        check_reportable(graph, task)
    with pytest.raises(ReportRefused, match="nothing is built yet"):
        check_reportable(graph, node)
    with pytest.raises(ReportRefused, match="the feature has no branch yet"):
        find_target(project, node)


def test_the_target_is_the_feature_branch_and_a_stable_path(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, step, _, branch = built(code_repo, project, "Write")
    check_reportable(load_graph(project), node)
    target = find_target(project, step)  # started on the step: its feature
    assert (target.feature, target.branch, target.home) == (node, branch, "main")
    assert target.sha == tip(code_repo, branch)
    assert target.path == f"docs/features/export-{handle(node)}.md"
    assert target.detail == "standard"

    # Once written, the report keeps its file when the feature is renamed.
    commit_report(project, target, "# Export\n")
    update_node(project, node, title="Export and import")
    assert find_target(project, node).path == target.path


def test_a_branch_with_no_work_has_nothing_to_report(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, _, (task,) = feature(project, "Write")
    choose_base(project, task)  # creates the feature branch, empty
    set_status(project, task, Status.IN_PROGRESS)
    with pytest.raises(ReportRefused, match="has no work on it yet"):
        find_target(project, node)


def test_the_detail_level_comes_from_the_feature_s_settings(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, _, _, _ = built(code_repo, project, "Write")
    config = project.apm_dir / "apm.yaml"
    override = f"    {node}: {{report_detail: detailed}}\n"
    config.write_text(
        config.read_text() + "features:\n  report_detail: brief\n  overrides:\n" + override
    )
    assert find_target(project, node).detail == "detailed"


def test_the_report_is_a_new_commit_pushed_on_top(code_repo: CodeRepo) -> None:
    project = code_repo.project
    node, _, _, branch = built(code_repo, project, "Write")
    before = tip(code_repo, branch)
    target = find_target(project, node)
    worktree = prepare_worktree(project, target)
    assert git(worktree, ["rev-parse", "HEAD"]).strip() == before

    sha = commit_report(project, target, "# Export\n\nFirst.\n", worktree=worktree)
    assert tip(code_repo, branch) == sha  # pushed to origin
    assert git(code_repo.dir, ["rev-parse", f"{sha}^"]).strip() == before  # on top, no force
    shown = git(code_repo.dir, ["show", f"{sha}:{target.path}"])
    assert shown == "# Export\n\nFirst.\n"
    (entry,) = log(code_repo.dir, f"{before}..{sha}")
    assert entry.subject == "Add the feature report for Export"
    assert entry.trailers["APM-Node"] == node

    again = commit_report(project, target, "# Export\n\nSecond.\n", worktree=worktree)
    assert git(code_repo.dir, ["rev-parse", f"{again}^"]).strip() == sha
    assert (
        log(code_repo.dir, f"{sha}..{again}")[0].subject == "Update the feature report for Export"
    )
    # The same report again changes nothing.
    assert commit_report(project, target, "# Export\n\nSecond.\n", worktree=worktree) == again


def test_a_branch_that_moved_meanwhile_gets_the_report_on_its_new_tip(
    code_repo: CodeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = code_repo.project
    node, _, _, branch = built(code_repo, project, "Write")
    target = find_target(project, node)
    later = code_repo.push_branch("late", base=f"origin/{branch}")
    git(code_repo.dir, ["push", "--quiet", "origin", f"{later}:refs/heads/{branch}"])

    # And someone pushes again between APM's fetch and its push: it starts over, once.
    real_push = report_branch._push
    calls = []

    def racing(*args: object) -> None:
        calls.append(args)
        if len(calls) == 1:
            raise GitError(["push"], 1, "rejected: non-fast-forward")
        real_push(*args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(report_branch, "_push", racing)
    sha = commit_report(project, target, "# Export\n")
    assert len(calls) == 2
    assert git(code_repo.dir, ["rev-parse", f"{sha}^"]).strip() == later


def test_a_report_that_can_t_be_committed_is_kept_with_why(
    code_repo: CodeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = code_repo.project
    node, _, _, branch = built(code_repo, project, "Write")
    target = find_target(project, node)
    before = tip(code_repo, branch)

    def refused(*_: object) -> None:
        raise GitError(["push"], 1, "remote: permission denied")

    monkeypatch.setattr(report_branch, "_push", refused)
    store = Store(project.apm_dir)
    saved = save_report(
        project, store, target, ReportAnswer(summary="S.", markdown="# E"), run="r1"
    )
    assert saved.sha is None
    assert saved.error is not None
    assert "permission denied" in saved.error
    assert saved.markdown.startswith("# E\n\n---\n")
    assert store.latest_report(node) == saved
    assert tip(code_repo, branch) == before  # the branch is left as it was


def test_the_pull_request_links_the_report_once(code_repo: CodeRepo, fake_gh: FakeGh) -> None:
    project = code_repo.project
    pr = fake_gh.add(7, body="The spec.\n\nAPM-Node: x\n")
    path = "docs/features/export-1.md"
    url = report_url(project, "feature/1-export", path, pr)
    assert url == "https://github.com/o/r/blob/feature/1-export/docs/features/export-1.md"
    link_report(project, pr, path, url)
    body = fake_gh.pr(7)["body"]
    assert body == f"**Feature report:** [{path}]({url})\n\nThe spec.\n\nAPM-Node: x\n"
    link_report(project, pr, path, url + "?v=2")  # a rewrite replaces the link
    assert fake_gh.pr(7)["body"].count("**Feature report:**") == 1
    assert "?v=2" in fake_gh.pr(7)["body"]


def test_without_a_pull_request_the_url_comes_from_the_remote(code_repo: CodeRepo) -> None:
    project = code_repo.project  # remote: https://github.com/o/r in apm.yaml
    assert report_url(project, "feature/x", "docs/a.md") == (
        "https://github.com/o/r/blob/feature/x/docs/a.md"
    )
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text().replace("https://github.com/o/r", "git@gitlab.com:o/r"))
    assert report_url(project, "feature/x", "docs/a.md") is None


def test_with_link_puts_the_link_first() -> None:
    assert with_link("", "**Feature report:** x") == "**Feature report:** x\n"
    assert with_link("Body\n", "**Feature report:** x") == "**Feature report:** x\n\nBody\n"
    twice = with_link("**Feature report:** old\n\nBody\n", "**Feature report:** new")
    assert twice == "**Feature report:** new\n\nBody\n"


def test_a_nested_feature_s_report_goes_on_its_own_branch(code_repo: CodeRepo) -> None:
    project = code_repo.project
    outer, _, (first,) = feature(project, "Write")
    (inner,) = create_nodes(project, [NewNode(title="Import", parent=outer)]).created
    (task,) = decompose(project, inner, [NewNode(title="Read")]).created
    inner_branch = choose_base(project, task).branch
    assert inner_branch is not None
    sha = code_repo.push_branch(f"apm/{task[-6:]}", base=f"origin/{inner_branch}")
    git(code_repo.dir, ["push", "--quiet", "origin", f"{sha}:refs/heads/{inner_branch}"])
    attach_delivery(project, task, branch=f"apm/{task[-6:]}", commits=[sha])
    target = find_target(project, inner)
    outer_branch = choose_base(project, first).branch
    assert (target.branch, target.home) == (inner_branch, outer_branch)
