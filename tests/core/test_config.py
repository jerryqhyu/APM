import re
from pathlib import Path

import pytest

from apm.core.config import ConfigError, default_repo, parse_config, read_config

FIXTURES = Path(__file__).parent.parent / "fixtures"
MINIMAL = "name: x\ncode: { repos: { app: { path: '..' } } }\n"
DEFAULT_RUNNER = {
    "max_concurrent": 2,
    "default_budget_usd": 2.0,
    "permission_mode": "auto",
    "agents": {},
}
DEFAULT_AUTOMATION = {
    "task_review": True,
    "reconcile": True,
    "reconcile_interval_s": 300.0,
    "feature_review": True,
    "accept_follow_ups": True,
    "implement_follow_ups": True,
    "feature_pr": True,
    "eager_redo": True,
}
DEFAULT_FEATURES = {
    "eager": False,
    "task_max_lines": 50,
    "report": True,
    "report_detail": "standard",
    "budget_usd": None,
    "overrides": {},
}


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
            "permission_mode": "auto",
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
        "automation": DEFAULT_AUTOMATION,
        "features": DEFAULT_FEATURES,
    }


def test_fills_defaults_and_a_single_repo_is_the_default() -> None:
    config = parse_config("name: x\ncode:\n  repos:\n    app: { path: '..' }\n")
    assert config.code.repos["app"].model_dump() == {"path": "..", "remote": None, "default": True}
    assert config.runner.model_dump() == DEFAULT_RUNNER
    assert config.automation.model_dump() == DEFAULT_AUTOMATION
    assert config.features.model_dump() == DEFAULT_FEATURES
    assert default_repo(config) == "app"


def test_each_automation_switch_can_be_set_alone() -> None:
    config = parse_config(
        f"{MINIMAL}automation: {{ feature_pr: false, reconcile_interval_s: 60 }}\n"
    )
    expected = {**DEFAULT_AUTOMATION, "feature_pr": False, "reconcile_interval_s": 60.0}
    assert config.automation.model_dump() == expected


def test_reads_feature_defaults_and_overrides() -> None:
    config = parse_config(
        f"{MINIMAL}features:\n  eager: true\n  task_max_lines: 80\n  report_detail: brief\n"
        "  overrides:\n    0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000: { report: false }\n"
        "    '01a1fe93':\n      task_max_lines: 30\n    12e45678:\n"
    )
    assert config.features.model_dump(exclude={"overrides"}) == {
        "eager": True,
        "task_max_lines": 80,
        "report": True,
        "report_detail": "brief",
        "budget_usd": None,
    }
    overrides = {k: v.model_dump(exclude_none=True) for k, v in config.features.overrides.items()}
    # An override with nothing in it is no override, like any other empty key.
    assert overrides == {
        "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000": {"report": False},
        "01a1fe93": {"task_max_lines": 30},
    }


def test_eager_redo_is_an_automation_switch() -> None:
    config = parse_config(f"{MINIMAL}automation: {{ eager_redo: false }}\n")
    assert config.automation.eager_redo is False


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
        (f"{REPOS}automation: {{ feature_prs: false }}", r"automation\.feature_prs: "),
        (f"{REPOS}automation: {{ task_review: 'no' }}", r"automation\.task_review: "),
        (f"{REPOS}automation: {{ reconcile: 1 }}", r"automation\.reconcile: "),
        (
            f"{REPOS}automation: {{ reconcile_interval_s: 0 }}",
            r"automation\.reconcile_interval_s: ",
        ),
        (
            "name: x\ncode: { repos: { 'My Repo': { path: a } } }",
            r"code\.repos\.My Repo: invalid key: must be lowercase",
        ),
        (f"{REPOS}features: {{ eager: 'yes' }}", r"features\.eager: "),
        (f"{REPOS}features: {{ task_max_lines: 4 }}", r"features\.task_max_lines: .* 5"),
        (f"{REPOS}features: {{ task_max_lines: 50.5 }}", r"features\.task_max_lines: "),
        (f"{REPOS}features: {{ report_detail: huge }}", r"features\.report_detail: .*'brief'"),
        (f"{REPOS}features: {{ eagr: true }}", r"features\.eagr: "),
        (
            f"{REPOS}features: {{ overrides: {{ my-feature: {{ eager: true }} }} }}",
            r"features\.overrides\.my-feature: invalid key: must be a node id or handle",
        ),
        (
            f"{REPOS}features: {{ overrides: {{ 01a1fe93: {{ eagr: true }} }} }}",
            r"features\.overrides\.01a1fe93\.eagr: ",
        ),
        (
            f"{REPOS}features: {{ overrides: {{ 01a1fe93: {{ task_max_lines: 2 }} }} }}",
            r"features\.overrides\.01a1fe93\.task_max_lines: ",
        ),
        (
            f"{REPOS}features: {{ overrides: {{ 12345678: {{ eager: true }} }} }}",
            r"features\.overrides: put the handle 12345678 in quotes, or use the full node id",
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
        "an unknown automation switch",
        "a quoted automation switch",
        "a number for an automation switch",
        "a zero reconcile interval",
        "a quoted boolean for eager",
        "a task limit below 5",
        "a fractional task limit",
        "an unknown report detail",
        "an unknown features setting",
        "an override key that is no handle",
        "an unknown setting in an override",
        "a too small limit in an override",
        "an unquoted handle of digits",
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
