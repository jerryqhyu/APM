import re
from pathlib import Path

import pytest

from apm.core.config import ConfigError, default_repo, parse_config, read_config

FIXTURES = Path(__file__).parent.parent / "fixtures"
MINIMAL = "name: x\ncode: { repos: { app: { path: '..' } } }\n"
DEFAULT_RUNNER = {"max_concurrent": 2, "default_budget_usd": 2.0, "agents": {}}


def issues_for(text: str) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        parse_config(text)
    return list(caught.value.issues)


def test_parses_the_example_from_plans_md() -> None:
    # The example in plans.md §3.4, verbatim.
    config = parse_config((FIXTURES / "apm.example.yaml").read_text())
    no_agent = {"model": None, "budget_usd": None, "allowed_tools": None}
    assert config.model_dump() == {
        "name": "my-app",
        "code": {
            "repos": {
                "app": {"path": "..", "remote": "git@github.com:jerry/app.git", "default": True}
            }
        },
        "runner": {
            "max_concurrent": 2,
            "default_budget_usd": 2.0,
            "agents": {
                "apm-decomposer": {**no_agent, "model": "opus"},
                "apm-critic": {**no_agent, "model": "sonnet"},
                "apm-implementer": {
                    "model": "opus",
                    "budget_usd": 5.0,
                    "allowed_tools": ["Bash(git *)", "Bash(npm test*)"],
                },
            },
        },
    }


def test_fills_defaults_and_a_single_repo_is_the_default() -> None:
    config = parse_config("name: x\ncode:\n  repos:\n    app: { path: '..' }\n")
    assert config.code.repos["app"].model_dump() == {"path": "..", "remote": None, "default": True}
    assert config.runner.model_dump() == DEFAULT_RUNNER
    assert default_repo(config) == "app"


def test_fills_defaults_inside_a_partial_runner_section() -> None:
    config = parse_config(f"{MINIMAL}runner: {{ max_concurrent: 4 }}\n")
    assert config.runner.model_dump() == {**DEFAULT_RUNNER, "max_concurrent": 4}


@pytest.mark.parametrize(
    "runner",
    [
        "runner:\n",
        "runner:\n  # max_concurrent: 4\n",
        "runner:\n  agents:\n",
        "runner:\n  max_concurrent:\n  default_budget_usd: 2\n",
    ],
    ids=["empty runner", "everything commented out", "empty agents", "an empty setting"],
)
def test_treats_empty_keys_as_not_set(runner: str) -> None:
    assert parse_config(MINIMAL + runner).runner.model_dump() == DEFAULT_RUNNER


def test_treats_an_empty_agent_entry_and_an_empty_remote_as_not_set() -> None:
    config = parse_config(
        "name: x\ncode:\n  repos:\n    app: { path: '..', remote: }\nrunner:\n  agents:\n"
        "    apm-critic:\n    apm-decomposer: { model: opus, budget_usd: }\n"
    )
    assert config.code.repos["app"].remote is None
    assert list(config.runner.agents) == ["apm-decomposer"]
    assert config.runner.agents["apm-decomposer"].budget_usd is None


def test_still_requires_required_keys_that_are_present_but_empty() -> None:
    assert issues_for("name:\ncode: { repos: { app: { path: '..' } } }\n")[0].startswith("name: ")
    assert issues_for("name: x\ncode:\n")[0].startswith("code: ")


def test_picks_the_marked_default_among_several_repos() -> None:
    config = parse_config(
        "name: x\ncode:\n  repos:\n    api: { path: '../api' }\n"
        "    web: { path: '../web', default: true }\n"
    )
    assert default_repo(config) == "web"
    assert config.code.repos["api"].default is False


REPOS = "name: x\ncode: { repos: { a: { path: a } } }\n"


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        (
            "name: x\ncode: { repos: { a: { path: a }, b: { path: b } } }",
            r"code\.repos: exactly one",
        ),
        (
            "name: x\ncode: { repos: { a: { path: a, default: true }, "
            "b: { path: b, default: true } } }",
            r"exactly one repo",
        ),
        ("name: x\ncode: { repos: { a: { path: a, default: false } } }", r"exactly one repo"),
        ("name: x\ncode: { repos: {} }", r"at least one repo"),
        (f"{REPOS}runnr: {{}}", r"runnr"),
        (f"{REPOS}runner: {{ default_budget_usd: -1 }}", r"runner\.default_budget_usd: "),
        (f"{REPOS}runner: {{ max_concurrent: 1.5 }}", r"runner\.max_concurrent: "),
        (f"{REPOS}runner: {{ max_concurrent: '2' }}", r"runner\.max_concurrent: "),
        ("name: x\ncode: { repos: { a: { path: a, default: 'yes' } } }", r"default: "),
        (
            "name: x\ncode: { repos: { 'My Repo': { path: a } } }",
            r"code\.repos\.My Repo: invalid key: must be lowercase",
        ),
        ("code: { repos: { a: { path: a } } }", r"^name: "),
        ("", r"^name: "),
        ("- just\n- a list\n", r"valid dictionary"),
    ],
    ids=[
        "no default among several repos",
        "two defaults",
        "a single repo marked not default",
        "no repos",
        "an unknown key (typo)",
        "a negative budget",
        "a fractional max_concurrent",
        "a quoted number",
        "a quoted boolean",
        "a bad repo key",
        "a missing name",
        "an empty file",
        "a list instead of a mapping",
    ],
)
def test_rejects(text: str, pattern: str) -> None:
    issues = issues_for(text)
    assert any(re.search(pattern, issue) for issue in issues), issues


def test_reports_yaml_syntax_errors_with_line_and_column() -> None:
    assert re.match(r"\d+:\d+: ", issues_for("name: x\ncode: [\n")[0])


def test_read_config_reads_apm_yaml_or_reports_it_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"^apm\.yaml: file not found$"):
        read_config(tmp_path)
    (tmp_path / "apm.yaml").write_text(MINIMAL)
    assert read_config(tmp_path).name == "x"
