"""`apm report`: write a feature's report by hand, and show the latest one."""

import pytest

from apm.core.git import git
from apm.core.ids import handle
from tests.conftest import CodeRepo
from tests.core.test_feature_review import built, feature
from tests.runner.fake import FakeClaude
from tests.test_cli import apm


@pytest.fixture
def repo(code_repo: CodeRepo, monkeypatch: pytest.MonkeyPatch) -> CodeRepo:
    monkeypatch.chdir(code_repo.dir)
    return code_repo


def test_write_a_report_and_show_it(repo: CodeRepo, fake_claude: FakeClaude) -> None:
    node, _, _, branch = built(repo, repo.project, "Write")
    assert apm("report", handle(node), "--show").strip() == "no report yet"

    said = apm("report", handle(node), "-m", "Mention the format.")
    assert "▶ apm-feature-reporter" in said
    path = f"docs/features/export-{handle(node)}.md"
    assert f"APM: Writing the report from `{branch}`" in said
    assert f"APM: **Report written**: committed to `{branch}`" in said
    assert "## Guidance from the user\n\nMention the format." in fake_claude.calls[0]["prompt"]
    sha = git(repo.dir, ["rev-parse", f"origin/{branch}"]).strip()

    shown = apm("report", handle(node), "--show")
    assert shown.startswith(f"{path} on {branch}, commit {sha[:7]} (standard)\n")
    assert f"https://github.com/o/r/blob/{branch}/{path}\n" in shown
    assert "\n# Search\n\nA standard report" in shown


def test_a_report_that_can_t_be_written_says_why(repo: CodeRepo, fake_claude: FakeClaude) -> None:
    node, _, (task,) = feature(repo.project, "Write")
    assert "only a feature has a report" in apm("report", handle(task), code=1)
    assert "nothing is built yet" in apm("report", handle(node), code=1)
    node2, _, _, _ = built(repo, repo.project, "Read")
    fake_claude.mode("report-fails")
    assert "✗ failed" in apm("report", handle(node2), code=1)
