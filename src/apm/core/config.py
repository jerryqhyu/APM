"""apm.yaml (plans.md §3.4). Unknown keys are errors, so typos surface instead of being ignored."""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

from apm.core.errors import ApmError, format_issue
from apm.core.files import CONFIG_FILE, cached_parse, config_path, read_file_if_exists
from apm.core.ids import could_name_a_node


class ConfigError(ApmError, ValueError):
    def __init__(self, issues: Sequence[str], source: str = CONFIG_FILE) -> None:
        super().__init__("\n".join(f"{source}: {issue}" for issue in issues))
        self.issues = tuple(issues)
        self.source = source


_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _check_key(key: str) -> str:
    if _KEY_RE.fullmatch(key) is None:
        raise ValueError("must be lowercase letters, digits, - or _")
    return key


Key = Annotated[str, AfterValidator(_check_key)]
NonEmpty = Annotated[str, Field(min_length=1)]
Positive = Annotated[float, Field(gt=0)]


class _Model(BaseModel):
    # Strict: YAML's `"2"` is not a number and `1.5` is not an int.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RepoConfig(_Model):
    path: NonEmpty
    """Relative to the plan repo."""
    remote: NonEmpty | None = None
    default: bool = False


class AgentConfig(_Model):
    model: NonEmpty | None = None
    budget_usd: Positive | None = None
    allowed_tools: list[NonEmpty] | None = None


class CodeConfig(_Model):
    repos: dict[Key, RepoConfig]

    @field_validator("repos", mode="before")
    @classmethod
    def _single_repo_is_default(cls, repos: Any) -> Any:
        if isinstance(repos, dict) and len(repos) == 1:
            ((key, repo),) = repos.items()
            if isinstance(repo, dict) and "default" not in repo:
                return {key: {**repo, "default": True}}
        return repos

    @field_validator("repos")
    @classmethod
    def _exactly_one_default(cls, repos: dict[str, RepoConfig]) -> dict[str, RepoConfig]:
        if not repos:
            raise ValueError("at least one repo is required")
        if sum(repo.default for repo in repos.values()) != 1:
            raise ValueError("exactly one repo must have default: true")
        return repos


class RunnerConfig(_Model):
    max_concurrent: Annotated[int, Field(gt=0)] = 2
    default_budget_usd: Positive = 2.0
    permission_mode: Literal["auto", "manual", "acceptEdits", "plan", "dontAsk"] = "auto"
    """What agents start in unless you pick another mode for a run."""
    agents: dict[Key, AgentConfig] = Field(default_factory=dict)


MIN_TASK_LINES = 5
"""The smallest task size limit: below it, hardly any change fits in one task."""
type ReportDetail = Literal["brief", "standard", "detailed"]
TaskLines = Annotated[int, Field(ge=MIN_TASK_LINES)]


def _check_node_key(key: str) -> str:
    if not could_name_a_node(key):
        raise ValueError("must be a node id or handle")
    return key


NodeKey = Annotated[str, AfterValidator(_check_node_key)]


class FeatureOverride(_Model):
    """One feature's own settings. Each one left out is inherited from the feature around it, or
    from the project.
    """

    eager: bool | None = None
    task_max_lines: TaskLines | None = None
    report: bool | None = None
    report_detail: ReportDetail | None = None


class FeaturesConfig(_Model):
    """How features are built and reported: the project's defaults, and per-feature overrides.
    `apm.core.settings.feature_settings` works out what applies to a node.
    """

    eager: bool = False
    """Build a feature in one run on one branch, then split it into one pull request per task."""
    task_max_lines: TaskLines = 50
    """The size limit a task review applies, in real lines of source code."""
    report: bool = True
    """Write a feature report once the feature's review passes."""
    report_detail: ReportDetail = "standard"
    """How much that report says."""
    overrides: dict[NodeKey, FeatureOverride] = Field(default_factory=dict)
    """Per feature, by node id (a handle works too, typed by hand): the settings that differ from
    the defaults above. They apply to everything inside the feature, unless a feature inside it
    overrides them in turn.
    """

    @field_validator("overrides", mode="before")
    @classmethod
    def _keys_are_text(cls, overrides: Any) -> Any:
        # YAML reads an unquoted handle of digits only as a number, and one starting with 0 as an
        # octal number: the handle it meant is lost, so say how to write it.
        if isinstance(overrides, dict):
            for key in overrides:
                if not isinstance(key, str):
                    raise ValueError(f"put the handle {key} in quotes, or use the full node id")
        return overrides


class AutomationConfig(_Model):
    """What `apm serve` does on its own. Each step has its own switch; all are on by default."""

    task_review: bool = True
    """Review a task's size whenever it is created or its title, kind or description changes."""
    reconcile: bool = True
    """Reconcile (and retarget open PRs) every `reconcile_interval_s` seconds."""
    reconcile_interval_s: Positive = 300.0
    feature_review: bool = True
    """Start a feature's review once its review step becomes ready."""
    accept_follow_ups: bool = True
    """Accept the follow-up tasks a feature review proposes."""
    implement_follow_ups: bool = True
    """Build those follow-ups once their task review passes."""
    feature_pr: bool = True
    """Open the feature's pull request into its home once its feature review passes."""
    eager_redo: bool = True
    """When a task pull request of an eager feature changes, rebuild and re-split the tasks after
    it, on its own (`features.eager`). With it off, changed tasks are still shown as stale, and
    Redo rebuilds them by hand.
    """


class ApmConfig(_Model):
    name: NonEmpty
    code: CodeConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)


def parse_config(text: str, source: str = CONFIG_FILE) -> ApmConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.MarkedYAMLError as err:
        mark = err.problem_mark or err.context_mark
        where = f"{mark.line + 1}:{mark.column + 1}: " if mark else ""
        raise ConfigError([f"{where}{err.problem or err.context}"], source) from None
    except yaml.YAMLError as err:
        raise ConfigError([str(err)], source) from None
    try:
        return ApmConfig.model_validate(_drop_nulls({} if data is None else data))
    except ValidationError as err:
        raise ConfigError([format_issue(e) for e in err.errors()], source) from None


def read_config(apm_dir: Path) -> ApmConfig:
    """apm.yaml, parsed again only when it changes."""
    return cached_parse(config_path(apm_dir), _parse_file)


def _parse_file(path: Path) -> ApmConfig:
    text = read_file_if_exists(path)
    if text is None:
        raise ConfigError(["file not found"])
    return parse_config(text)


def default_repo(config: ApmConfig) -> str:
    """The key of the repo marked default."""
    return next(key for key, repo in config.code.repos.items() if repo.default)


def _drop_nulls(value: Any) -> Any:
    """YAML reads an empty key (`runner:` with its body commented out) as null. Treat it as not set,
    so defaults apply, by dropping null-valued keys from every mapping.
    """
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value
