"""What settings apply to a node, and changing them in apm.yaml."""

import pytest

from apm.core.config import ConfigError, parse_config
from apm.core.git import git, log
from apm.core.graph import Graph
from apm.core.ids import HandleError, handle
from apm.core.mutate import OpError
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project
from apm.core.settings import (
    FeatureSettings,
    clear_override,
    feature_settings,
    override_problems,
    resolve_settings,
    set_defaults,
    set_override,
)
from apm.core.views import node_history
from tests.core.build import A, B, C, D, E, make_id, node

REPO = "name: x\ncode: { repos: { app: { path: '..' } } }\n"
DEFAULTS = FeatureSettings(eager=False, task_max_lines=50, report=True, report_detail="standard")

# A feature A holding a feature B, which holds the task C; D is a task of A; E is elsewhere.
GRAPH = Graph([node(A), node(B, A), node(C, B), node(D, A), node(E)])


def config(features: str = ""):
    return parse_config(REPO + (f"features:\n{features}" if features else ""))


# --- what applies to a node ---


def test_without_a_features_section_every_node_gets_the_defaults() -> None:
    for node_id in (A, B, C, D, E):
        assert feature_settings(config(), GRAPH, node_id) == DEFAULTS


def test_the_projects_defaults_apply_everywhere() -> None:
    settings = feature_settings(config("  eager: true\n  task_max_lines: 80\n"), GRAPH, C)
    assert settings == FeatureSettings(True, 80, True, "standard")


def test_a_features_override_applies_to_it_and_to_everything_inside() -> None:
    cfg = config(f"  overrides:\n    {A}: {{ eager: true, report_detail: detailed }}\n")
    overridden = FeatureSettings(True, 50, True, "detailed")
    assert [feature_settings(cfg, GRAPH, n) for n in (A, B, C, D)] == [overridden] * 4
    assert feature_settings(cfg, GRAPH, E) == DEFAULTS


def test_an_inner_override_wins_for_what_it_sets() -> None:
    cfg = config(
        "  task_max_lines: 60\n  overrides:\n"
        f"    {A}: {{ eager: true, task_max_lines: 40 }}\n"
        f"    {B}: {{ task_max_lines: 20, report: false }}\n"
    )
    assert feature_settings(cfg, GRAPH, C) == FeatureSettings(True, 20, False, "standard")
    assert feature_settings(cfg, GRAPH, D) == FeatureSettings(True, 40, True, "standard")
    resolved = resolve_settings(cfg, GRAPH, C)
    assert resolved.sources == {
        "eager": A,
        "task_max_lines": B,
        "report": B,
        "report_detail": None,
    }
    assert resolved.own is None
    assert resolve_settings(cfg, GRAPH, B).own is not None


def test_a_node_can_override_its_own_settings() -> None:
    cfg = config(f"  overrides:\n    {C}: {{ task_max_lines: 25 }}\n")
    assert feature_settings(cfg, GRAPH, C).task_max_lines == 25
    assert feature_settings(cfg, GRAPH, B).task_max_lines == 50


def test_handles_in_overrides_name_their_nodes() -> None:
    cfg = config(f"  overrides:\n    '{handle(B)}': {{ eager: true }}\n    {E[-5:]}: {{}}\n")
    assert feature_settings(cfg, GRAPH, C).eager is True
    assert feature_settings(cfg, GRAPH, A).eager is False


def test_overrides_that_name_no_node_are_ignored_and_reported() -> None:
    gone = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
    graph = Graph([*GRAPH.nodes.values(), node(make_id(0x1ABCD)), node(make_id(0x2ABCD))])
    cfg = config(
        f"  overrides:\n    {gone}: {{ eager: true }}\n    abcd: {{ eager: true }}\n"
        f"    {B}: {{ task_max_lines: 30 }}\n    '{handle(B)}': {{ task_max_lines: 99 }}\n"
    )
    assert feature_settings(cfg, graph, C) == FeatureSettings(False, 30, True, "standard")
    assert override_problems(cfg, graph) == [
        f"features.overrides.{gone} matches no node: it is ignored",
        "features.overrides.abcd names several nodes: it is ignored",
        f"features.overrides.{handle(B)} is a second override of {handle(B)}: only the first"
        " one counts",
    ]


def test_an_unknown_node_is_an_error() -> None:
    with pytest.raises(KeyError):
        feature_settings(config(), GRAPH, "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000")


# --- changing them ---


def apm_yaml(project: Project) -> str:
    return (project.apm_dir / "apm.yaml").read_text()


def write_apm_yaml(project: Project, text: str) -> None:
    (project.apm_dir / "apm.yaml").write_text(text)
    git(project.apm_dir, ["commit", "--quiet", "-am", "hand-written apm.yaml"])


def feature(project: Project) -> tuple[str, str]:
    top = create_nodes(project, [NewNode(title="Search")]).created[0]
    task = create_nodes(project, [NewNode(title="Index", parent=top)]).created[0]
    return top, task


def test_defaults_are_changed_and_committed_keeping_comments(project: Project) -> None:
    before = apm_yaml(project)
    write_apm_yaml(project, f"# My project\n{before}automation:\n  reconcile: true  # keep\n")
    change = set_defaults(
        project, features={"eager": True, "task_max_lines": 80}, automation={"task_review": False}
    )
    assert change.sha is not None
    assert change.config.features.eager is True
    assert change.config.automation.task_review is False
    text = apm_yaml(project)
    assert text.startswith("# My project\n")
    assert "  reconcile: true  # keep\n  task_review: false\n" in text
    assert text.endswith("features:\n  eager: true\n  task_max_lines: 80\n")
    entry = log(project.apm_dir)[0]
    assert entry.subject == (
        "settings: eager mode on, task size limit 80 lines, automation task_review off"
    )
    assert entry.trailers["APM-Op"] == "settings"
    assert entry.trailers["APM-Actor"] == "user"
    assert git(project.apm_dir, ["status", "--porcelain"]) == ""


def test_setting_what_is_already_set_commits_nothing(project: Project) -> None:
    set_defaults(project, features={"eager": True})
    commits = len(log(project.apm_dir))
    assert set_defaults(project, features={"eager": True}).sha is None
    assert len(log(project.apm_dir)) == commits


def test_a_bad_value_is_refused_and_the_file_left_alone(project: Project) -> None:
    before, commits = apm_yaml(project), len(log(project.apm_dir))
    with pytest.raises(ConfigError, match=r"features\.task_max_lines: .*greater than or equal to 5"):
        set_defaults(project, features={"task_max_lines": 3})
    with pytest.raises(ConfigError, match=r"features\.report_detail: "):
        set_defaults(project, features={"report_detail": "huge"})
    with pytest.raises(OpError, match=r"unknown setting features\.overrides"):
        set_defaults(project, features={"overrides": "x"})
    with pytest.raises(OpError, match=r"unknown setting automation\.nope"):
        set_defaults(project, automation={"nope": True})
    assert apm_yaml(project) == before
    assert len(log(project.apm_dir)) == commits


def test_a_broken_apm_yaml_is_refused(project: Project) -> None:
    (project.apm_dir / "apm.yaml").write_text(REPO + "runner: { max_concurrent: 0 }\n")
    with pytest.raises(ConfigError, match=r"runner\.max_concurrent"):
        set_defaults(project, features={"eager": True})


def test_hand_edits_to_apm_yaml_are_committed_with_the_change(project: Project) -> None:
    (project.apm_dir / "apm.yaml").write_text(apm_yaml(project) + "runner:\n  max_concurrent: 3\n")
    set_defaults(project, features={"report": False})
    assert "max_concurrent: 3" in git(project.apm_dir, ["show", "HEAD:apm.yaml"])
    assert "edits made to apm.yaml by hand" in git(project.apm_dir, ["log", "-1", "--format=%b"])
    assert git(project.apm_dir, ["status", "--porcelain"]) == ""


def test_none_puts_a_default_back(project: Project) -> None:
    initial = apm_yaml(project)
    set_defaults(project, features={"eager": True})
    set_defaults(project, features={"eager": None})
    assert apm_yaml(project) == initial
    assert log(project.apm_dir)[0].subject == "settings: eager mode back to default"


def test_a_features_override_is_set_inherited_and_cleared(project: Project) -> None:
    top, task = feature(project)
    initial = apm_yaml(project)

    change = set_override(project, top, {"eager": True, "report_detail": "detailed"})
    assert change.config.features.overrides[top].eager is True
    assert log(project.apm_dir)[0].subject == (
        f'settings {handle(top)}: eager mode on, report detail detailed for "Search"'
    )
    assert log(project.apm_dir)[0].trailers["APM-Nodes"] == top
    assert node_history(project, top)[0].op == "settings"
    assert node_history(project, task) == node_history(project, task)[-1:]  # only its creation

    set_override(project, top, {"eager": None})
    assert log(project.apm_dir)[0].subject.startswith(f"settings {handle(top)}: eager mode inherited")
    overrides = parse_config(apm_yaml(project)).features.overrides
    assert overrides[top].model_dump(exclude_none=True) == {"report_detail": "detailed"}

    clear_override(project, top)
    assert apm_yaml(project) == initial
    assert log(project.apm_dir)[0].subject == (
        f'settings {handle(top)}: own settings cleared for "Search"'
    )
    assert clear_override(project, top).sha is None


def test_an_override_typed_with_a_handle_is_edited_where_it_is(project: Project) -> None:
    top, _ = feature(project)
    write_apm_yaml(
        project,
        apm_yaml(project) + f"features:\n  overrides:\n    '{handle(top)}':  # by hand\n"
        "      eager: true\n",
    )
    set_override(project, handle(top), {"task_max_lines": 30})
    text = apm_yaml(project)
    assert f"'{handle(top)}':  # by hand\n      eager: true\n      task_max_lines: 30\n" in text
    assert top not in text
    clear_override(project, top)
    assert "overrides" not in apm_yaml(project)


def test_an_override_refuses_bad_values_and_unknown_nodes(project: Project) -> None:
    top, _ = feature(project)
    with pytest.raises(ConfigError, match=r"features\.overrides\..*\.task_max_lines"):
        set_override(project, top, {"task_max_lines": 1})
    with pytest.raises(OpError, match="unknown setting features.automation"):
        set_override(project, top, {"automation": True})
    with pytest.raises(HandleError, match="no node matches"):
        set_override(project, "abcdef12", {"eager": True})
    assert "features" not in apm_yaml(project)
