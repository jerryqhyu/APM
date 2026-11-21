"""A stand-in for the `gh` CLI, for the parts of APM that open pull requests.

It answers the two REST calls APM makes, and `gh pr view`:

- `gh api -X GET repos/<o>/<r>/pulls ...`: the open pull requests, from FAKE_GH_OPEN (JSON, `[]`
  by default).
- `gh api -X POST repos/<o>/<r>/pulls ...`: a new pull request, `https://github.com/o/r/pull/7`.
- FAKE_GH=fail: every call fails the way GitHub refuses a pull request with no changes.

Every call's arguments are appended to FAKE_GH_LOG as one JSON line.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

NEW_PR = "https://github.com/o/r/pull/7"


def main() -> int:
    args = sys.argv[1:]
    with Path(os.environ["FAKE_GH_LOG"]).open("a") as log:
        log.write(json.dumps(args) + "\n")
    if os.environ.get("FAKE_GH") == "fail":
        refusal = {"message": "Validation Failed", "errors": [{"message": "No commits between"}]}
        print(json.dumps(refusal))
        print("gh: Validation Failed (HTTP 422)", file=sys.stderr)
        return 1
    if args[:3] == ["api", "-X", "GET"]:
        print(os.environ.get("FAKE_GH_OPEN", "[]"))
        return 0
    if args[:3] == ["api", "-X", "POST"]:
        print(json.dumps({"html_url": NEW_PR, "number": 7}))
        return 0
    print(f"fake gh: unexpected call {args}", file=sys.stderr)
    return 1


class FakeGh:
    """Puts the fake `gh` first on PATH and reads back what it was called with."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.bin = tmp_path / "gh-bin"
        self.bin.mkdir()
        self.log = tmp_path / "gh-calls.jsonl"
        script = self.bin / "gh"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{__file__}" "$@"\n')
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_GH_LOG", str(self.log))
        monkeypatch.delenv("FAKE_GH", raising=False)
        monkeypatch.delenv("FAKE_GH_OPEN", raising=False)

    def fail(self) -> None:
        self._monkeypatch.setenv("FAKE_GH", "fail")

    def open_prs(self, *urls: str) -> None:
        self._monkeypatch.setenv("FAKE_GH_OPEN", json.dumps([{"html_url": u} for u in urls]))

    def missing(self, tmp_path: Path) -> None:
        """No `gh` at all: PATH holds only git."""
        only_git = tmp_path / "only-git"
        only_git.mkdir()
        git = shutil.which("git")
        assert git is not None
        (only_git / "git").symlink_to(git)
        self._monkeypatch.setenv("PATH", str(only_git))

    @property
    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def field(self, call: list[str], name: str) -> str:
        """The value of `-f <name>=...` in a call."""
        prefix = f"{name}="
        return next(a.removeprefix(prefix) for a in call if a.startswith(prefix))


if __name__ == "__main__":
    sys.exit(main())
