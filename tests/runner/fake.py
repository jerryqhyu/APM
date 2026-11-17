import json
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).with_name("fake_claude.py")


class FakeClaude:
    """Points the runner at the fake `claude` and reads back what it was called with."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.log = tmp_path / "claude-calls.jsonl"
        script = tmp_path / "claude"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        script.chmod(0o755)
        monkeypatch.setenv("APM_CLAUDE", str(script))
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self.log))
        monkeypatch.delenv("FAKE_CLAUDE", raising=False)

    def mode(self, *modes: str) -> None:
        self._monkeypatch.setenv("FAKE_CLAUDE", ",".join(modes))

    @property
    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]
