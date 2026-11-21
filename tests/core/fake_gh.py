"""A stand-in for `gh api`, for reconcile and delivery tests. Never talks to GitHub.

It answers `gh api [--hostname H] [-X METHOD] repos/O/R/pulls/N [-f key=value ...]` from a JSON
file of pull requests keyed by `repos/O/R/pulls/N`, applies PATCHes to that file, and appends
every call's arguments to a log, one JSON line each.
"""

import json
import os
import sys
from pathlib import Path

import pytest


def main() -> int:
    state = Path(os.environ["FAKE_GH_STATE"])
    with Path(os.environ["FAKE_GH_LOG"]).open("a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\n")
    args = sys.argv[1:]
    if not args or args[0] != "api":
        print("unsupported: only `gh api` is faked", file=sys.stderr)
        return 1
    args = args[1:]
    method, fields, path = "GET", {}, None
    while args:
        arg = args.pop(0)
        if arg == "--hostname":
            args.pop(0)
        elif arg == "-X":
            method = args.pop(0)
        elif arg == "-f":
            key, _, value = args.pop(0).partition("=")
            fields[key] = value
        else:
            path = arg
    prs = json.loads(state.read_text())
    if path not in prs:
        print(json.dumps({"message": "Not Found"}))
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        return 1
    if method == "PATCH":
        if "base" in fields:
            prs[path]["base"]["ref"] = fields["base"]
        state.write_text(json.dumps(prs))
    print(json.dumps(prs[path]))
    return 0


class FakeGh:
    """Installs the fake on PATH. `add` registers a pull request; `calls` lists what ran."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "fake-gh-bin"
        bin_dir.mkdir()
        self.state = tmp_path / "fake-gh-prs.json"
        self.log = tmp_path / "fake-gh-calls.ndjson"
        self.state.write_text("{}")
        self.log.write_text("")
        script = bin_dir / "gh"
        script.write_text(
            f"#!{sys.executable}\nimport runpy\nrunpy.run_path({__file__!r}, run_name='__main__')\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_GH_STATE", str(self.state))
        monkeypatch.setenv("FAKE_GH_LOG", str(self.log))

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
        """Registers PR `number` and returns its URL."""
        prs = json.loads(self.state.read_text())
        prs[f"repos/{repo}/pulls/{number}"] = {
            "number": number,
            "state": state,
            "merged": merged,
            "merge_commit_sha": merge_commit,
            "base": {"ref": base},
            "html_url": f"https://github.com/{repo}/pull/{number}",
        }
        self.state.write_text(json.dumps(prs))
        return f"https://github.com/{repo}/pull/{number}"

    def pr(self, number: int, repo: str = "o/r") -> dict:
        return json.loads(self.state.read_text())[f"repos/{repo}/pulls/{number}"]

    @property
    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    sys.exit(main())
