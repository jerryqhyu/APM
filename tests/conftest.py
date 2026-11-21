import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm.core.git import CommitMessage, add, commit, git
from apm.core.project import Project, init_project
from tests.fake_gh import FakeGh
from tests.runner.fake import FakeClaude


@pytest.fixture(scope="session", autouse=True)
def _isolated_git() -> Iterator[None]:
    """Gives git a fixed identity and hides the machine's own config (signing, hooks, templates),
    so commits made by the code under test behave the same everywhere, child processes included.
    """
    settings = {
        "GIT_AUTHOR_NAME": "APM Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "APM Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    saved = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    os.environ.pop("APM_PROJECT", None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@dataclass(frozen=True)
class TempRepo:
    dir: Path

    def commit_file(self, path: str, content: str, subject: str | None = None) -> str:
        """Writes a file (relative to the repo), stages it and commits; returns the SHA."""
        (self.dir / path).write_text(content, encoding="utf-8", newline="")
        add(self.dir, [path])
        return commit(self.dir, CommitMessage(subject=subject or f"write {path}"))


@pytest.fixture
def temp_repo(tmp_path: Path) -> TempRepo:
    """An empty repo on branch `main`."""
    git(tmp_path, ["init", "--quiet", "-b", "main"])
    return TempRepo(tmp_path)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A freshly initialised project in a code directory called `my-app`."""
    code_dir = tmp_path / "my-app"
    code_dir.mkdir()
    return init_project(code_dir)


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    """Points the runner at a stand-in for the `claude` CLI."""
    return FakeClaude(tmp_path, monkeypatch)


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    """Puts a stand-in for the `gh` CLI first on PATH."""
    return FakeGh(tmp_path, monkeypatch)


@dataclass(frozen=True)
class CodeRepo:
    """The project's code directory as a git repo on `main`, pushed to a local `origin`."""

    project: Project

    @property
    def dir(self) -> Path:
        return self.project.code_dir

    def commit(self, name: str) -> str:
        (self.dir / name).write_text(name)
        git(self.dir, ["add", name])
        git(self.dir, ["commit", "--quiet", "-m", f"add {name}"])
        return git(self.dir, ["rev-parse", "HEAD"]).strip()

    def push_branch(self, branch: str, base: str = "main") -> str:
        """A branch off `base` with one commit, pushed to `origin`. Returns to `main`."""
        git(self.dir, ["checkout", "--quiet", "-b", branch, base])
        sha = self.commit(f"{branch.replace('/', '-')}.txt")
        git(self.dir, ["push", "--quiet", "-u", "origin", branch])
        git(self.dir, ["checkout", "--quiet", "main"])
        return sha

    def merge(self, branch: str) -> None:
        """Merges `branch` into `main` and pushes `main`."""
        git(self.dir, ["merge", "--quiet", "--no-ff", "-m", f"merge {branch}", branch])
        git(self.dir, ["push", "--quiet", "origin", "main"])


@pytest.fixture
def code_repo(project: Project, tmp_path: Path) -> CodeRepo:
    """The project's code directory as a git repo with a bare `origin`, and a GitHub remote in
    apm.yaml (nothing ever talks to it: pair with `fake_gh`).
    """
    origin = tmp_path / "origin.git"
    git(tmp_path, ["init", "--quiet", "--bare", "-b", "main", str(origin)])
    repo = CodeRepo(project)
    git(repo.dir, ["init", "--quiet", "-b", "main"])
    repo.commit("README.md")
    git(repo.dir, ["remote", "add", "origin", str(origin)])
    git(repo.dir, ["push", "--quiet", "-u", "origin", "main"])
    git(repo.dir, ["remote", "set-head", "origin", "main"])
    config = project.apm_dir / "apm.yaml"
    config.write_text(
        config.read_text().replace(
            "      path: ..\n", "      path: ..\n      remote: https://github.com/o/r\n"
        )
    )
    git(project.apm_dir, ["commit", "--quiet", "-am", "remote"])
    return repo
