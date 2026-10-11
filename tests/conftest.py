from dataclasses import dataclass
from pathlib import Path

import pytest

from apm.core.git import CommitMessage, add, commit, git


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
    """An empty repo on branch `main`, with a local identity and signing off, so tests don't depend
    on the machine's git config.
    """
    git(tmp_path, ["init", "--quiet", "-b", "main"])
    git(tmp_path, ["config", "user.name", "APM Test"])
    git(tmp_path, ["config", "user.email", "test@example.com"])
    git(tmp_path, ["config", "commit.gpgsign", "false"])
    return TempRepo(tmp_path)
