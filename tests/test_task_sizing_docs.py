"""The task reviewer's agent and skill ship with the rules the reviewer must follow."""

from pathlib import Path

import pytest

from apm.runner.agents import PLUGIN_DIR

AGENT = PLUGIN_DIR / "agents" / "apm-task-reviewer.md"
SKILL = PLUGIN_DIR / "skills" / "apm-task-sizing" / "SKILL.md"
FIXTURES = Path(__file__).parent / "fixtures" / "task_review"


def frontmatter(path: Path) -> dict[str, str]:
    _, head, _ = path.read_text().split("---\n", 2)
    return dict(line.split(": ", 1) for line in head.strip().splitlines())


def test_the_agent_takes_its_limit_from_the_prompt() -> None:
    text = AGENT.read_text()
    assert "the limit your first message gives" in text
    assert "50 real lines" not in text


def test_the_agent_is_read_only_and_uses_the_sizing_skill() -> None:
    meta = frontmatter(AGENT)
    assert meta["name"] == "apm-task-reviewer"
    disallowed = {tool.strip() for tool in meta["disallowedTools"].split(",")}
    assert {"Write", "Edit", "NotebookEdit"} <= disallowed
    assert "tools" not in meta
    assert "apm-task-sizing" in meta["skills"]


def test_the_skill_defines_real_lines_and_the_limit() -> None:
    assert frontmatter(SKILL)["name"] == "apm-task-sizing"
    text = SKILL.read_text().lower()
    # The limit is the one the prompt gives (the project's setting), 50 when none is given.
    assert "use the limit your prompt gives for the task" in text
    assert "features.task_max_lines" in text
    assert "default: **at most 50 real lines**" in text
    excluded = text.split("## what counts as a real line", 1)[1].split("##", 1)[0]
    for word in ("blank lines", "comments", "imports", "tests", "docs", "skill and agent markdown"):
        assert word in excluded


@pytest.mark.parametrize(
    ("verdict", "heading"),
    [
        ("pass", "about 20 lines"),
        ("too_big", "more than 100"),
        ("unclear", "a one-line vague title"),
    ],
)
def test_the_skill_works_an_example_of_each_verdict(verdict: str, heading: str) -> None:
    assert f"### {verdict}: {heading}" in SKILL.read_text()


@pytest.mark.parametrize("verdict", ["pass", "too_big", "unclear"])
def test_a_fixture_task_exists_for_each_verdict(verdict: str) -> None:
    meta = frontmatter(FIXTURES / f"{verdict}.md")
    assert meta["expected"] == verdict
    assert meta["title"]
