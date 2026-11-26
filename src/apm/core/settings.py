"""The settings that apply to a feature or a task, and changing them (plans.md §3.4).

`features:` in apm.yaml holds the project's defaults and per-feature overrides. `feature_settings`
is the one place that works out what applies to a node: the defaults, overridden by each feature
around it that has an override, the innermost last, and finally by the node's own. The task
review, the decomposer and the implementer read the task size limit through it; eager mode and the
feature report read theirs the same way.

The changes below edit apm.yaml in place, keeping its comments and formatting
(`apm.core.config_edit`), refuse a value `parse_config` wouldn't accept, and commit the file to the
plan repo like any other change to the plan: one commit each, under the write lock.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apm.core.config import (
    ApmConfig,
    AutomationConfig,
    ConfigError,
    FeatureOverride,
    FeaturesConfig,
    ReportDetail,
    parse_config,
    read_config,
)
from apm.core.config_edit import KeyPath, Scalar, edit_yaml
from apm.core.files import CONFIG_FILE, read_file_if_exists
from apm.core.git import CommitMessage, git
from apm.core.graph import Graph
from apm.core.ids import HandleError, handle
from apm.core.lock import acquire_write_lock
from apm.core.model import NodeId
from apm.core.mutate import OpError, write_and_commit
from apm.core.project import Project, load_graph

type SettingName = Literal["eager", "task_max_lines", "report", "report_detail"]
SETTINGS: tuple[SettingName, ...] = ("eager", "task_max_lines", "report", "report_detail")
"""What `features:` sets, project-wide or per feature."""
AUTOMATION: tuple[str, ...] = tuple(AutomationConfig.model_fields)


@dataclass(frozen=True, slots=True)
class FeatureSettings:
    """The settings that apply to one node."""

    eager: bool
    """Build the feature in one run, then split it into one pull request per task."""
    task_max_lines: int
    """The task size limit, in real lines of source code."""
    report: bool
    """Write a feature report once its feature review passes."""
    report_detail: ReportDetail
    """How much that report says: brief, standard or detailed."""


@dataclass(frozen=True, slots=True)
class ResolvedSettings:
    """`feature_settings`, with where each value comes from."""

    settings: FeatureSettings
    sources: Mapping[SettingName, NodeId | None]
    """The node whose override sets each value; None for the project's default."""
    own: FeatureOverride | None
    """The node's own override, if it has one."""


def feature_settings(config: ApmConfig, graph: Graph, node_id: NodeId) -> FeatureSettings:
    """The settings that apply to a node: the project's `features:` defaults, overridden by the
    nearest enclosing feature (or the node itself) that has an override. Inner overrides win.

    Works for a task (its feature's settings, and those of the features above it) and for a
    feature. Raises `KeyError` for a node that isn't in `graph`.
    """
    return resolve_settings(config, graph, node_id).settings


def resolve_settings(
    config: ApmConfig, graph: Graph, node_id: NodeId, *, inherited: bool = False
) -> ResolvedSettings:
    """`feature_settings`, with where each value comes from: what the UI shows. With `inherited`,
    what the node would get without its own override.
    """
    defaults = config.features
    values: dict[SettingName, Any] = {name: getattr(defaults, name) for name in SETTINGS}
    sources: dict[SettingName, NodeId | None] = dict.fromkeys(SETTINGS)
    overrides = overrides_by_node(config, graph)
    owners = (
        graph.ancestors(node_id) if inherited else [graph[node_id].id, *graph.ancestors(node_id)]
    )
    for owner in reversed(owners):
        override = overrides.get(owner)
        for name in SETTINGS if override is not None else ():
            if (value := getattr(override, name)) is not None:
                values[name], sources[name] = value, owner
    return ResolvedSettings(FeatureSettings(**values), sources, overrides.get(node_id))


def overrides_by_node(config: ApmConfig, graph: Graph) -> dict[NodeId, FeatureOverride]:
    """The overrides in apm.yaml by the node they apply to, handles resolved. A key that names no
    node (deleted, or mistyped) or several is ignored, and so is a second key for the same node:
    the first one in the file counts. `override_problems` lists them.
    """
    out: dict[NodeId, FeatureOverride] = {}
    for key, override in config.features.overrides.items():
        node_id = _named(graph, key)
        if node_id is not None and node_id not in out:
            out[node_id] = override
    return out


def override_problems(config: ApmConfig, graph: Graph) -> list[str]:
    """Overrides in apm.yaml that apply to nothing, each as one line for `apm check`."""
    problems, seen = [], set()
    for key in config.features.overrides:
        try:
            node_id = graph.resolve(key)
        except HandleError as err:
            reason = "names several nodes" if err.code == "ambiguous" else "matches no node"
            problems.append(f"features.overrides.{key} {reason}: it is ignored")
            continue
        if node_id in seen:
            problems.append(
                f"features.overrides.{key} is a second override of {handle(node_id)}: only the"
                " first one counts"
            )
        seen.add(node_id)
    return problems


def check_config(apm_dir: Path, graph: Graph) -> tuple[list[str], list[str]]:
    """What `apm check` says about apm.yaml: its errors, or else the overrides that apply to
    nothing, each line starting with the file's name.
    """
    try:
        config = read_config(apm_dir)
    except ConfigError as err:
        return [f"{CONFIG_FILE}: {issue}" for issue in err.issues], []
    return [], [f"{CONFIG_FILE}: {problem}" for problem in override_problems(config, graph)]


def _named(graph: Graph, key: str) -> NodeId | None:
    try:
        return graph.resolve(key)
    except HandleError:
        return None


def _override_key(config: ApmConfig, graph: Graph, node_id: NodeId) -> str:
    """Where a node's override lives in apm.yaml: the key the file already has for it (a handle,
    if that is what was typed), else its full id.
    """
    keys = (key for key in config.features.overrides if _named(graph, key) == node_id)
    return next(keys, node_id)


# --- changing the settings ---


@dataclass(frozen=True, slots=True)
class SettingsChange:
    sha: str | None
    """The new commit in the plan repo, or None if nothing changed."""
    config: ApmConfig
    """apm.yaml as it is now."""


type Decision = tuple[Mapping[KeyPath, Scalar | None], str, NodeId | None]
"""What to change in apm.yaml, the commit's subject, and the node it is about."""


def set_defaults(
    project: Project,
    *,
    features: Mapping[str, Scalar | None] | None = None,
    automation: Mapping[str, Scalar | None] | None = None,
    actor: str = "user",
) -> SettingsChange:
    """Changes project-wide settings: `features:` defaults and `automation:` switches, by name.
    None puts a setting back to APM's own default. One commit, or none if nothing changed.
    """
    features, automation = features or {}, automation or {}
    _check_names("features", features, SETTINGS)
    _check_names("automation", automation, AUTOMATION)
    changes: dict[KeyPath, Scalar | None] = {
        **{("features", name): value for name, value in features.items()},
        **{("automation", name): value for name, value in automation.items()},
    }
    said = [*_describe(features, unset="back to default"), *_describe_automation(automation)]
    return _change(project, lambda *_: (changes, f"settings: {', '.join(said)}", None), actor)


def set_override(
    project: Project,
    node_id: NodeId,
    values: Mapping[str, Scalar | None],
    *,
    actor: str = "user",
) -> SettingsChange:
    """Changes one feature's own settings, by name: None means inherit it again. An override with
    nothing left in it is removed. A task can have one too, though features are what they're for.
    """
    _check_names("features", values, SETTINGS)
    said = ", ".join(_describe(values, unset="inherited"))

    def decide(config: ApmConfig, graph: Graph) -> Decision:
        node = graph[graph.resolve(node_id)]
        key = _override_key(config, graph, node.id)
        changes: dict[KeyPath, Scalar | None] = {
            ("features", "overrides", key, name): value for name, value in values.items()
        }
        return changes, f'settings {handle(node.id)}: {said} for "{node.title}"', node.id

    return _change(project, decide, actor)


def clear_override(project: Project, node_id: NodeId, *, actor: str = "user") -> SettingsChange:
    """Removes a feature's own settings, so it inherits them all again."""

    def decide(config: ApmConfig, graph: Graph) -> Decision:
        node = graph[graph.resolve(node_id)]
        keys = [key for key in config.features.overrides if _named(graph, key) == node.id]
        changes: dict[KeyPath, Scalar | None] = {
            ("features", "overrides", key): None for key in keys
        }
        subject = f'settings {handle(node.id)}: own settings cleared for "{node.title}"'
        return changes, subject, node.id

    return _change(project, decide, actor)


def _check_names(section: str, values: Mapping[str, object], known: tuple[str, ...]) -> None:
    if unknown := [name for name in values if name not in known]:
        raise OpError(f"unknown setting {section}.{unknown[0]}: pick one of {', '.join(known)}")


def _change(
    project: Project, decide: Callable[[ApmConfig, Graph], Decision], actor: str
) -> SettingsChange:
    """Edits apm.yaml as `decide` says, given the config and the plan as they are, and commits
    it; all under the write lock. A value `parse_config` refuses raises `ConfigError`, and the
    file is left as it was.
    """
    apm_dir = project.apm_dir
    with acquire_write_lock(apm_dir):
        text = read_file_if_exists(apm_dir / CONFIG_FILE) or ""
        # A broken apm.yaml is the user's to fix first.
        changes, subject, node = decide(parse_config(text), load_graph(project))
        edited = edit_yaml(text, changes) if changes else text
        config = parse_config(edited)
        if edited == text:
            return SettingsChange(None, config)
        trailers = [("APM-Op", "settings"), *([("APM-Nodes", node)] if node else [])]
        trailers.append(("APM-Actor", actor))
        body = "Also records the edits made to apm.yaml by hand." if _hand_edited(project) else ""
        message = CommitMessage(subject=subject, body=body, trailers=trailers)
        sha = write_and_commit(apm_dir, {CONFIG_FILE: edited}, message, over_hand_edits=True)
        return SettingsChange(sha, config)


def _hand_edited(project: Project) -> bool:
    return bool(git(project.apm_dir, ["status", "--porcelain", "--", CONFIG_FILE]).strip())


# --- commit subjects ---

_SAID: dict[str, str] = {
    "eager": "eager mode",
    "task_max_lines": "task size limit",
    "report": "feature report",
    "report_detail": "report detail",
}


def _describe(values: Mapping[str, Scalar | None], *, unset: str) -> list[str]:
    """`{"eager": True, "task_max_lines": 80}` as ["eager mode on", "task size limit 80 lines"]."""
    out = []
    for name, value in values.items():
        if value is None:
            out.append(f"{_SAID[name]} {unset}")
        elif isinstance(value, bool):
            out.append(f"{_SAID[name]} {'on' if value else 'off'}")
        elif name == "task_max_lines":
            out.append(f"{_SAID[name]} {value} lines")
        else:
            out.append(f"{_SAID[name]} {value}")
    return out


def _describe_automation(values: Mapping[str, Scalar | None]) -> list[str]:
    out = []
    for name, value in values.items():
        if value is None:
            out.append(f"automation {name} back to default")
        elif isinstance(value, bool):
            out.append(f"automation {name} {'on' if value else 'off'}")
        else:
            out.append(f"automation {name} {value}")
    return out


def settings_view(config: ApmConfig, graph: Graph) -> dict[str, Any]:
    """The project's settings for the UI: defaults, automation, and every override."""
    defaults = FeaturesConfig.model_fields
    return {
        "features": config.features.model_dump(exclude={"overrides"}),
        "automation": config.automation.model_dump(),
        "defaults": {
            "features": {name: defaults[name].default for name in SETTINGS},
            "automation": {n: f.default for n, f in AutomationConfig.model_fields.items()},
        },
        "overrides": [
            {
                "key": key,
                "node": _named(graph, key),
                "values": override.model_dump(exclude_none=True),
            }
            for key, override in config.features.overrides.items()
        ],
        "problems": override_problems(config, graph),
    }
