"""apm.yaml (plans.md §3.4). Unknown keys are errors, so typos surface instead of being ignored."""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

from apm.core.files import CONFIG_FILE, config_path, read_file_if_exists
from apm.core.issues import format_issue


class ConfigError(ValueError):
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
    agents: dict[Key, AgentConfig] = Field(default_factory=dict)


class ApmConfig(_Model):
    name: NonEmpty
    code: CodeConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)


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
    text = read_file_if_exists(config_path(apm_dir))
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
