import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm.core.git import CommitMessage, add, commit, git
from apm.core.project import Project, init_project
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
