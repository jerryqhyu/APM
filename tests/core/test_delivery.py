import os
from pathlib import Path

import pytest

from apm.core.delivery import (
    Base,
    add_note,
    choose_base,
    ensure_pull_request,
    github_repo,
    note_missing_pr,
)
from apm.core.files import read_body
from apm.core.git import git
from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.project import Project, load_graph
from tests.conftest import CodeRepo
from tests.fake_gh import NEW_PR, FakeGh


def chain(project: Project, *titles: str) -> tuple[str, ...]:
    """Nodes where each depends on the one before."""
    previous = (None, *titles[:-1])
    nodes = [
        NewNode(title=title, key=title, depends_on_keys=[before] if before else [])
        for before, title in zip(previous, titles, strict=True)
    ]
    return create_nodes(project, nodes).created


def delivered(project: Project, node: str, branch: str, **delivery: str) -> None:
    attach_delivery(project, node, branch=branch, **delivery)
    set_status(project, node, Status.DONE)


# --- the base branch ---


def test_a_node_with_no_dependencies_starts_from_the_default_branch(code_repo: CodeRepo) -> None:
    (node,) = create_nodes(code_repo.project, [NewNode(title="Alone")]).created
    assert choose_base(code_repo.project, node) == Base("main")


def test_a_node_stacks_on_its_dependency_s_unmerged_branch(code_repo: CodeRepo) -> None:
    first, second = chain(code_repo.project, "First", "Second")
    code_repo.push_branch("apm/first")
    delivered(code_repo.project, first, "apm/first")
    assert choose_base(code_repo.project, second) == Base("apm/first", stacked_on=first)


def test_a_merged_or_deleted_dependency_branch_is_not_stacked_on(code_repo: CodeRepo) -> None:
    first, second = chain(code_repo.project, "First", "Second")
    code_repo.push_branch("apm/first")
    delivered(code_repo.project, first, "apm/first")
    code_repo.merge("apm/first")
    assert choose_base(code_repo.project, second) == Base("main")

    # Deleted on the remote after it merged elsewhere: the fetch prunes it, so it's gone.
    third = create_nodes(code_repo.project, [NewNode(title="Third", depends_on=[second])]).created
    code_repo.push_branch("apm/second")
    delivered(code_repo.project, second, "apm/second")
    git(code_repo.dir, ["push", "--quiet", "origin", "--delete", "apm/second"])
    git(code_repo.dir, ["branch", "--quiet", "-D", "apm/second"])
    assert choose_base(code_repo.project, third[0]) == Base("main")


def test_a_merged_pull_request_counts_as_merged(
    code_repo: CodeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A squash merge leaves the branch's own commits off main: only the PR says it merged.
    first, second = chain(code_repo.project, "First", "Second")
    code_repo.push_branch("apm/first")
    delivered(code_repo.project, first, "apm/first", pr="https://github.com/o/r/pull/1")
    bin_dir = tmp_path / "pr-view"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text('#!/bin/sh\necho \'{"state": "MERGED"}\'\n')
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert choose_base(code_repo.project, second) == Base("main")


def test_several_unmerged_dependencies_mean_the_default_branch(code_repo: CodeRepo) -> None:
    project = code_repo.project
    a, b = create_nodes(project, [NewNode(title="A"), NewNode(title="B")]).created
    (both,) = create_nodes(project, [NewNode(title="Both", depends_on=[a, b])]).created
    for node, branch in ((a, "apm/a"), (b, "apm/b")):
        code_repo.push_branch(branch)
        delivered(project, node, branch)
    base = choose_base(project, both)
    assert base.branch == "main"
    assert base.stacked_on is None
    assert set(base.unmerged) == {(a, "apm/a"), (b, "apm/b")}


def test_a_dependency_of_an_ancestor_counts_too(code_repo: CodeRepo) -> None:
    project = code_repo.project
    (dep,) = create_nodes(project, [NewNode(title="Dep")]).created
    (parent,) = create_nodes(project, [NewNode(title="Parent", depends_on=[dep])]).created
    (child,) = create_nodes(project, [NewNode(title="Child", parent=parent)]).created
    code_repo.push_branch("apm/dep")
    delivered(project, dep, "apm/dep")
    assert choose_base(project, child) == Base("apm/dep", stacked_on=dep)


def test_a_code_dir_that_is_not_a_repo_has_no_base(project: Project) -> None:
    (node,) = create_nodes(project, [NewNode(title="N")]).created
    assert choose_base(project, node) == Base(None)


# --- the pull request ---


def test_a_pull_request_the_agent_recorded_is_left_alone(
    code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    (node,) = create_nodes(code_repo.project, [NewNode(title="N")]).created
    attach_delivery(code_repo.project, node, branch="apm/n", pr="https://github.com/o/r/pull/3")
    outcome = ensure_pull_request(code_repo.project, node)
    assert (outcome.action, outcome.pr) == ("already", "https://github.com/o/r/pull/3")
    assert fake_gh.calls == []


def test_apm_opens_the_pull_request_against_the_stacked_base(
    code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    first, second = chain(project, "First", "Second step")
    code_repo.push_branch("apm/first")
    delivered(project, first, "apm/first")
    code_repo.push_branch("apm/second", base="apm/first")
    attach_delivery(project, second, branch="apm/second")

    outcome = ensure_pull_request(project, second, run="run-1")
    assert (outcome.action, outcome.pr, outcome.base) == ("opened", NEW_PR, "apm/first")
    listing, create = fake_gh.calls
    assert listing[:4] == ["api", "-X", "GET", "repos/o/r/pulls"]
    assert fake_gh.field(listing, "head") == "o:apm/second"
    assert create[:4] == ["api", "-X", "POST", "repos/o/r/pulls"]
    assert fake_gh.field(create, "title") == "Second step"
    assert fake_gh.field(create, "head") == "apm/second"
    assert fake_gh.field(create, "base") == "apm/first"
    assert f"APM-Node: {second}" in fake_gh.field(create, "body")
    assert "## Intent" in fake_gh.field(create, "body")
    node = load_graph(project)[second]
    assert node.delivery is not None
    assert node.delivery.pr == NEW_PR
    assert node.status is Status.TODO  # recording a PR marks nothing done
    assert "APM-Actor: apm" in git(project.apm_dir, ["log", "-1", "--format=%B"])


def test_an_open_pull_request_for_the_branch_is_recorded_not_duplicated(
    code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    (node,) = create_nodes(code_repo.project, [NewNode(title="N")]).created
    attach_delivery(code_repo.project, node, branch="apm/n")
    fake_gh.open_prs("https://github.com/o/r/pull/5")
    outcome = ensure_pull_request(code_repo.project, node)
    assert (outcome.action, outcome.pr) == ("found", "https://github.com/o/r/pull/5")
    assert [call[2] for call in fake_gh.calls] == ["GET"]


def test_when_github_refuses_the_notes_say_why_once(code_repo: CodeRepo, fake_gh: FakeGh) -> None:
    project = code_repo.project
    (node,) = create_nodes(project, [NewNode(title="N", body="## Intent\n\nDo it.\n")]).created
    attach_delivery(project, node, branch="apm/n")
    fake_gh.fail()
    outcome = ensure_pull_request(project, node)
    assert outcome.action is None
    assert outcome.problem == "`gh api` failed: No commits between"
    assert (outcome.branch, outcome.base) == ("apm/n", "main")
    delivery = load_graph(project)[node].delivery
    assert delivery is not None
    assert delivery.pr is None

    note_missing_pr(project, node, outcome, run=None)
    note_missing_pr(project, node, outcome, run=None)  # the same reason isn't added twice
    body = read_body(project.apm_dir, node)
    assert body.count("**No pull request yet.**") == 1
    assert "Branch `apm/n` is delivered" in body
    assert "against `main`: `gh api` failed: No commits between." in body
    assert body.startswith("## Intent\n\nDo it.\n")


def test_without_gh_or_a_branch_there_is_no_pull_request(
    code_repo: CodeRepo, fake_gh: FakeGh, tmp_path: Path
) -> None:
    project = code_repo.project
    (node,) = create_nodes(project, [NewNode(title="N")]).created
    no_branch = ensure_pull_request(project, node)
    assert no_branch.problem == "the run recorded no branch, so there is nothing to open one from"
    attach_delivery(project, node, branch="apm/n")
    fake_gh.missing(tmp_path)
    assert ensure_pull_request(project, node).problem == "`gh` is not installed"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/jerryqhyu/APM", ("jerryqhyu", "APM")),
        ("https://github.com/o/r.git", ("o", "r")),
        ("git@github.com:o/r.git", ("o", "r")),
        ("http://proxy@127.0.0.1:8080/git/o/r", ("o", "r")),
        ("nonsense", None),
        (None, None),
    ],
)
def test_the_github_repository_comes_from_the_remote(
    remote: str | None, expected: tuple[str, str] | None
) -> None:
    assert github_repo(remote) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", "## Notes\n\nX\n"),
        ("## Intent\n\nA\n", "## Intent\n\nA\n\n## Notes\n\nX\n"),
        ("## Intent\n\nA\n\n## Notes\n", "## Intent\n\nA\n\n## Notes\n\nX\n"),
        ("## Notes\n\nold\n\n## After\n\nB\n", "## Notes\n\nold\n\nX\n\n## After\n\nB\n"),
    ],
)
def test_a_note_goes_at_the_end_of_the_notes_section(body: str, expected: str) -> None:
    assert add_note(body, "X") == expected
