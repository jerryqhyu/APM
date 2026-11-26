"""Finding and creating a project (plans.md §3.1).

A project is a code repo plus its plan repo. The code repo commits a `.apm-link` file that says
where the plan repo is; the plan repo itself (`.apm/`) is ignored by the code repo.
"""

import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from apm.core.config import parse_config
from apm.core.errors import ApmError
from apm.core.files import (
    CONFIG_FILE,
    GRAPH_FILE,
    cached_parse,
    graph_path,
    read_graph_file,
    write_file_atomic,
)
from apm.core.git import CommitMessage, GitError, add, commit, git
from apm.core.graph import Graph
from apm.core.lock import CACHE_DIR

LINK_FILE = ".apm-link"
PROJECT_ENV = "APM_PROJECT"
DEFAULT_APM_DIR = ".apm"


class ProjectError(ApmError, RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Project:
    code_dir: Path
    """The directory that holds `.apm-link`."""
    apm_dir: Path
    """The plan repo."""


def open_project(code_dir: Path) -> Project:
    """The project whose `.apm-link` is in `code_dir`."""
    link = code_dir / LINK_FILE
    try:
        data = json.loads(link.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProjectError(f"no {LINK_FILE} in {code_dir}") from None
    except ValueError as err:
        raise ProjectError(f"{link}: invalid JSON: {err}") from None
    path = data.get("path") if isinstance(data, dict) else None
    if not isinstance(path, str) or not path:
        raise ProjectError(f'{link}: expected {{"path": "<plan repo>"}}')
    apm_dir = (code_dir / path).resolve()
    if not apm_dir.is_dir():
        raise ProjectError(f"{link} points at {apm_dir}, which doesn't exist (try `apm clone`)")
    return Project(code_dir.resolve(), apm_dir)


def find_project(start: Path | None = None, env: Mapping[str, str] | None = None) -> Project:
    """`APM_PROJECT` if it's set; otherwise the nearest `.apm-link` at or above `start` (default:
    the current directory).
    """
    env = os.environ if env is None else env
    if configured := env.get(PROJECT_ENV):
        return open_project(Path(configured))
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / LINK_FILE).is_file():
            return open_project(directory)
    raise ProjectError(f"not in an APM project: no {LINK_FILE} at or above {here} (try `apm init`)")


def init_project(code_dir: Path, *, name: str | None = None) -> Project:
    """Creates the plan repo at `<code_dir>/.apm` with its first commit, writes `.apm-link`, and
    adds `.apm/` to the code repo's `.gitignore`.
    """
    code_dir = code_dir.resolve()
    apm_dir = code_dir / DEFAULT_APM_DIR
    for existing in (code_dir / LINK_FILE, apm_dir):
        if existing.exists():
            raise ProjectError(f"{existing} already exists")
    name = name or code_dir.name
    config = _initial_config(name, _origin(code_dir))

    apm_dir.mkdir()
    try:
        git(apm_dir, ["init", "--quiet", "-b", "main"])
        write_file_atomic(apm_dir / CONFIG_FILE, config)
        write_file_atomic(apm_dir / GRAPH_FILE, "")
        write_file_atomic(apm_dir / ".gitignore", f"{CACHE_DIR}/\n")
        add(apm_dir, [CONFIG_FILE, GRAPH_FILE, ".gitignore"])
        trailers = [("APM-Op", "init"), ("APM-Actor", "user")]
        commit(apm_dir, CommitMessage(subject=f"init {name}", trailers=trailers))
    except BaseException:
        # For example git has no identity to commit with. Leave nothing half-made behind.
        shutil.rmtree(apm_dir, ignore_errors=True)
        raise

    return link_plan(code_dir)


def link_plan(code_dir: Path) -> Project:
    """Points `code_dir` at the plan repo in `<code_dir>/.apm`: writes `.apm-link` and keeps `.apm/`
    out of the code repo through its `.gitignore`.
    """
    link = {"path": DEFAULT_APM_DIR, "remote": None}
    write_file_atomic(code_dir / LINK_FILE, json.dumps(link) + "\n")
    _ignore(code_dir / ".gitignore", f"{DEFAULT_APM_DIR}/")
    # Where Claude Code puts the worktrees the implementer builds in.
    _ignore(code_dir / ".gitignore", ".claude/worktrees/")
    return Project(code_dir, code_dir / DEFAULT_APM_DIR)


def load_graph(project: Project) -> Graph:
    """The plan's graph, parsed again only when graph.ndjson changes."""
    return cached_parse(graph_path(project.apm_dir), _graph)


def _graph(path: Path) -> Graph:
    return Graph(read_graph_file(path.parent))


def _initial_config(name: str, remote: str | None) -> str:
    key = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_") or "app"
    repo: dict[str, object] = {"path": ".."}
    if remote:
        repo["remote"] = remote
    repo["default"] = True
    text = yaml.safe_dump(
        {"name": name, "code": {"repos": {key: repo}}}, sort_keys=False, allow_unicode=True
    )
    parse_config(text)  # never write a config that can't be read back
    return text


def _origin(code_dir: Path) -> str | None:
    """The code repo's `origin` URL, if it is a git repo and has one."""
    try:
        return git(code_dir, ["remote", "get-url", "origin"]).strip() or None
    except GitError:
        return None


def _ignore(gitignore: Path, entry: str) -> None:
    """Adds `entry` to a .gitignore unless it's already listed, keeping the rest as it is."""
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if entry in (line.strip() for line in text.splitlines()):
        return
    separator = "" if not text or text.endswith("\n") else "\n"
    write_file_atomic(gitignore, f"{text}{separator}{entry}\n")
