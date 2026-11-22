"""A stand-in for the `gh` CLI, for the parts of APM that open pull requests.

It answers the REST calls APM makes:

- `gh api -X GET repos/<o>/<r>/pulls ...`: the open pull requests, from FAKE_GH_OPEN (JSON, `[]`
  by default).
- `gh api -X POST repos/<o>/<r>/pulls ...`: a new pull request, `https://github.com/o/r/pull/7`.
  With FAKE_GH_NUMBERED (`FakeGh.numbered`), each one gets its own number from 101 up and is kept
  in FAKE_GH_PRS, open, with its base and head.
- `gh api [--hostname H] repos/<o>/<r>/pulls/<n>`: one pull request, from the JSON file at
  FAKE_GH_PRS (see `FakeGh.add`); 404 if it isn't there.
- `gh api -X PATCH repos/<o>/<r>/pulls/<n> -f base=<branch>`: retargets it in that file.
- FAKE_GH=fail: every call fails the way GitHub refuses a pull request with no changes.

Every call's arguments are appended to FAKE_GH_LOG as one JSON line.
"""

import json
import os
import re
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
    if (one := _one_pr(args)) is not None:
        return one
    if args[:3] == ["api", "-X", "GET"]:
        print(os.environ.get("FAKE_GH_OPEN", "[]"))
        return 0
    if args[:3] == ["api", "-X", "POST"]:
        if os.environ.get("FAKE_GH_NUMBERED"):
            print(json.dumps(_new_numbered_pr(args)))
            return 0
        print(json.dumps({"html_url": NEW_PR, "number": 7}))
        return 0
    print(f"fake gh: unexpected call {args}", file=sys.stderr)
    return 1


def _new_numbered_pr(args: list[str]) -> dict:
    """With FAKE_GH_NUMBERED: a new open pull request numbered from 101 up, kept in FAKE_GH_PRS
    with its head and base, so it can be read and retargeted afterwards.
    """
    fields = dict(a.partition("=")[::2] for a in args if "=" in a and not a.startswith("repos/"))
    repo = args[3].removeprefix("repos/").removesuffix("/pulls")
    store = Path(os.environ["FAKE_GH_PRS"])
    prs = json.loads(store.read_text()) if store.exists() else {}
    number = 101 + sum(1 for pr in prs.values() if pr.get("created"))
    pr = {
        "number": number,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "state": "open",
        "merged": False,
        "merge_commit_sha": None,
        "base": {"ref": fields.get("base", "")},
        "head": {"ref": fields.get("head", "")},
        "title": fields.get("title", ""),
        "body": fields.get("body", ""),
        "created": True,
    }
    prs[f"repos/{repo}/pulls/{number}"] = pr
    store.write_text(json.dumps(prs))
    return pr


_ONE_PR = re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+$")


def _one_pr(args: list[str]) -> int | None:
    """Answers a call about one pull request (read or retarget); None if it isn't one."""
    if not args or args[0] != "api":
        return None
    rest, method, fields, path = args[1:], "GET", {}, None
    while rest:
        arg = rest.pop(0)
        if arg in ("--hostname", "-X", "-f"):
            value = rest.pop(0)
            if arg == "-X":
                method = value
            elif arg == "-f":
                key, _, field = value.partition("=")
                fields[key] = field
        else:
            path = arg
    if path is None or not _ONE_PR.match(path):
        return None
    store = Path(os.environ.get("FAKE_GH_PRS", "/nonexistent"))
    prs = json.loads(store.read_text()) if store.exists() else {}
    if path not in prs:
        print(json.dumps({"message": "Not Found"}))
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        return 1
    if method == "PATCH" and "base" in fields:
        prs[path]["base"]["ref"] = fields["base"]
        store.write_text(json.dumps(prs))
    print(json.dumps(prs[path]))
    return 0


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
        self.prs = tmp_path / "gh-prs.json"
        monkeypatch.setenv("FAKE_GH_PRS", str(self.prs))
        monkeypatch.delenv("FAKE_GH", raising=False)
        monkeypatch.delenv("FAKE_GH_OPEN", raising=False)

    def fail(self) -> None:
        self._monkeypatch.setenv("FAKE_GH", "fail")

    def numbered(self) -> None:
        """Each new pull request gets its own number (101, 102, …) and can be read back."""
        self._monkeypatch.setenv("FAKE_GH_NUMBERED", "1")

    def merge(self, number: int, merge_commit: str, repo: str = "o/r") -> None:
        """Marks pull request `number` merged by `merge_commit`, into the base it has now."""
        prs = json.loads(self.prs.read_text())
        prs[f"repos/{repo}/pulls/{number}"] |= {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": merge_commit,
        }
        self.prs.write_text(json.dumps(prs))

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

    def add(
        self,
        number: int,
        *,
        base: str = "main",
        state: str = "open",
        merged: bool = False,
        merge_commit: str | None = None,
        repo: str = "o/r",
    ) -> str:
        """Makes pull request `number` exist, as `gh api .../pulls/<n>` will return it. Returns
        its URL.
        """
        prs = json.loads(self.prs.read_text()) if self.prs.exists() else {}
        url = f"https://github.com/{repo}/pull/{number}"
        prs[f"repos/{repo}/pulls/{number}"] = {
            "number": number,
            "html_url": url,
            "state": state,
            "merged": merged,
            "merge_commit_sha": merge_commit,
            "base": {"ref": base},
        }
        self.prs.write_text(json.dumps(prs))
        return url

    def pr(self, number: int, repo: str = "o/r") -> dict:
        """Pull request `number` as the fake holds it now."""
        return json.loads(self.prs.read_text())[f"repos/{repo}/pulls/{number}"]

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
