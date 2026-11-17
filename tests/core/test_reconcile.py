import json
import os
from pathlib import Path

import pytest

from apm.core.git import git, log
from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.project import Project, load_graph
from apm.core.reconcile import describe, reconcile


def code_commit(project: Project, name: str, branch: str | None = None) -> str:
    """Makes a commit in the code repo, optionally on a new branch, and returns its SHA."""
    code = project.code_dir
    if branch:
        git(code, ["checkout", "--quiet", "-b", branch])
    (code / name).write_text(name)
    git(code, ["add", name])
    git(code, ["commit", "--quiet", "-m", f"add {name}"])
    return git(code, ["rev-parse", "HEAD"]).strip()


@pytest.fixture
def code(project: Project) -> Project:
    """The project's code directory as a git repo on `main` with one commit."""
    git(project.code_dir, ["init", "--quiet", "-b", "main"])
    code_commit(project, "README.md")
    return project


def delivered(project: Project, title: str, **delivery: object) -> str:
    """A leaf that is in progress, with a delivery attached."""
    (node,) = create_nodes(project, [NewNode(title=title)]).created
    set_status(project, node, Status.IN_PROGRESS)
    attach_delivery(project, node, **delivery)  # ty: ignore[invalid-argument-type]
    return node


def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, states: dict[str, str]) -> None:
    """Puts a `gh` on PATH that answers `gh pr view <url> --json state` from `states`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        "#!/bin/sh\n"
        'case "$3" in\n'
        + "".join(
            f"  {url}) echo '{json.dumps({'state': state})}';;\n" for url, state in states.items()
        )
        + "  *) echo 'no such pr' >&2; exit 1;;\nesac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_a_node_whose_commits_are_on_main_becomes_done(code: Project) -> None:
    sha = code_commit(code, "feature.py", branch="apm/feature")
    merged = delivered(code, "Merged", branch="apm/feature", commits=[sha])
    other = code_commit(code, "other.py", branch="apm/other")
    unmerged = delivered(code, "Not merged", branch="apm/other", commits=[other])
    git(code.code_dir, ["checkout", "--quiet", "main"])
    git(code.code_dir, ["merge", "--quiet", "--no-ff", "-m", "merge feature", "apm/feature"])

    result = reconcile(code)
    assert [(r.node_id, r.reason) for r in result] == [(merged, "its commits are on main")]
    assert describe(result)[0].endswith("Merged: done (its commits are on main)")
    graph = load_graph(code)
    assert graph[merged].status is Status.DONE
    assert graph[unmerged].status is Status.IN_PROGRESS
    assert log(code.apm_dir)[0].subject.startswith("status ")
    assert "Reconciled: its commits are on main." in git(code.apm_dir, ["log", "-1", "--format=%b"])
    assert reconcile(code) == []  # nothing left to do


def test_a_merged_pull_request_wins_over_commit_ancestry(
    code: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A squash merge: the delivery commit itself never reaches main, but the PR is merged.
    sha = code_commit(code, "squashed.py", branch="apm/squashed")
    git(code.code_dir, ["checkout", "--quiet", "main"])
    merged = delivered(code, "Squashed", commits=[sha], pr="https://example.com/pull/1")
    still_open = delivered(code, "Open", pr="https://example.com/pull/2")
    fake_gh(
        tmp_path,
        monkeypatch,
        {"https://example.com/pull/1": "MERGED", "https://example.com/pull/2": "OPEN"},
    )
    assert [(r.node_id, r.reason) for r in reconcile(code)] == [(merged, "PR merged")]
    assert load_graph(code)[still_open].status is Status.IN_PROGRESS


def test_nodes_that_cannot_be_judged_are_left_alone(
    code: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh(tmp_path, monkeypatch, {})  # gh knows nothing
    unknown_pr = delivered(code, "Unknown PR", pr="https://example.com/pull/9")
    no_commits = delivered(code, "Branch only", branch="apm/x")
    missing = delivered(code, "Commit not in this clone", commits=["abcdef1234567"])
    (todo,) = create_nodes(code, [NewNode(title="Not started")]).created
    assert reconcile(code) == []
    graph = load_graph(code)
    assert {graph[n].status for n in (unknown_pr, no_commits, missing)} == {Status.IN_PROGRESS}
    assert graph[todo].status is Status.TODO
