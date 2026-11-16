import json
from pathlib import Path

import pytest

from apm.core.config import default_repo, read_config
from apm.core.git import git, log
from apm.core.project import (
    Project,
    ProjectError,
    find_project,
    init_project,
    load_graph,
    open_project,
)


def test_init_creates_the_plan_repo_with_one_commit(project: Project) -> None:
    apm = project.apm_dir
    assert apm == project.code_dir / ".apm"
    assert (apm / "graph.ndjson").read_text() == ""
    assert (apm / ".gitignore").read_text() == ".cache/\n"
    assert git(apm, ["branch", "--show-current"]).strip() == "main"
    assert git(apm, ["status", "--porcelain"]) == ""

    (entry,) = log(apm)
    assert entry.subject == "init my-app"
    assert entry.trailers == {"APM-Op": "init", "APM-Actor": "user"}
    assert len(load_graph(project)) == 0


def test_init_writes_a_config_that_reads_back(project: Project) -> None:
    config = read_config(project.apm_dir)
    assert config.name == "my-app"
    assert default_repo(config) == "my-app"
    assert config.code.repos["my-app"].path == ".."
    assert config.code.repos["my-app"].remote is None


def test_init_records_the_code_repos_origin_and_makes_a_safe_repo_key(tmp_path: Path) -> None:
    code_dir = tmp_path / "My App (v2)"
    code_dir.mkdir()
    git(code_dir, ["init", "--quiet"])
    git(code_dir, ["remote", "add", "origin", "git@example.com:me/app.git"])
    config = read_config(init_project(code_dir, name="Señor App").apm_dir)
    assert config.name == "Señor App"
    assert config.code.repos["se-or-app"].remote == "git@example.com:me/app.git"


def test_init_links_the_plan_repo_and_ignores_it(project: Project) -> None:
    code = project.code_dir
    assert json.loads((code / ".apm-link").read_text()) == {"path": ".apm", "remote": None}
    assert (code / ".gitignore").read_text() == ".apm/\n.claude/worktrees/\n"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("node_modules/\n", "node_modules/\n.apm/\n.claude/worktrees/\n"),
        ("dist", "dist\n.apm/\n.claude/worktrees/\n"),
        ("a\n.apm/\nb\n", "a\n.apm/\nb\n.claude/worktrees/\n"),
    ],
)
def test_init_adds_to_an_existing_gitignore_once(tmp_path: Path, before: str, after: str) -> None:
    (tmp_path / ".gitignore").write_text(before)
    init_project(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == after


def test_init_refuses_an_existing_project(project: Project) -> None:
    with pytest.raises(ProjectError, match="already exists"):
        init_project(project.code_dir)


def test_a_failed_init_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No identity, and no way to guess one: git refuses to commit.
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    with pytest.raises(RuntimeError, match="commit"):
        init_project(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_find_walks_up_from_a_subdirectory(project: Project) -> None:
    deep = project.code_dir / "src" / "pkg"
    deep.mkdir(parents=True)
    assert find_project(deep, env={}) == project
    assert find_project(project.code_dir, env={}) == project


def test_find_prefers_the_environment_variable(project: Project, tmp_path: Path) -> None:
    elsewhere = tmp_path / "worktree"
    elsewhere.mkdir()
    assert find_project(elsewhere, env={"APM_PROJECT": str(project.code_dir)}) == project


def test_find_fails_outside_a_project(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="not in an APM project"):
        find_project(tmp_path, env={})


def test_open_reports_a_broken_or_dangling_link(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match=r"no \.apm-link"):
        open_project(tmp_path)
    link = tmp_path / ".apm-link"
    link.write_text("{not json")
    with pytest.raises(ProjectError, match="invalid JSON"):
        open_project(tmp_path)
    link.write_text('{"remote": null}')
    with pytest.raises(ProjectError, match="expected"):
        open_project(tmp_path)
    link.write_text('{"path": ".apm"}')
    with pytest.raises(ProjectError, match="doesn't exist"):
        open_project(tmp_path)
