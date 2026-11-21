"""Getting a leaf's work into a pull request (plans.md §7.3).

Plain code around the implementer, not part of it:

- `choose_base`: where an implementer starts and which branch its pull request targets. A leaf
  that depends on unmerged work stacks on it, so its pull request shows only its own changes.
- `ensure_pull_request`: after an implementer run, opens the pull request itself if the agent
  pushed a branch but couldn't open one (no `gh`, or `gh pr` refused). It goes through the GitHub
  REST API (`gh api`), which works where `gh pr`'s GraphQL calls are blocked.

`gh` must be installed and signed in for anything here that talks to GitHub. Without it, every
function degrades to what local git can tell, and says why when that isn't enough.
"""

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apm.core.config import default_repo, read_config
from apm.core.errors import ApmError
from apm.core.files import read_body
from apm.core.git import GitError, git, is_ancestor, rev_parse
from apm.core.graph import Graph
from apm.core.model import NodeId, Status
from apm.core.ops import attach_delivery, edit_body
from apm.core.project import Project, load_graph

FETCH_TIMEOUT_S = 30.0
GH_TIMEOUT_S = 60.0
ACTOR = "apm"
"""Who changes the plan when APM itself records a pull request or a missing one."""


class GhError(ApmError, RuntimeError):
    """`gh` isn't there, or it failed. The message says which, in a few words."""


def gh(cwd: Path, args: Sequence[str], *, timeout: float = GH_TIMEOUT_S) -> str:
    """Runs `gh <args>` in `cwd` and returns its stdout. Raises `GhError` if it can't."""
    try:
        result = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        raise GhError("`gh` is not installed") from None
    except subprocess.TimeoutExpired:
        raise GhError(f"`gh {args[0]}` took longer than {timeout:g} s") from None
    except OSError as err:
        raise GhError(f"couldn't run `gh`: {err}") from None
    if result.returncode != 0:
        raise GhError(f"`gh {args[0]}` failed: {_gh_failure(result)}")
    return result.stdout


def _gh_failure(result: subprocess.CompletedProcess[str]) -> str:
    """The most useful line of a failed `gh` call: GitHub's own message if it sent one."""
    try:
        reply = json.loads(result.stdout)
        errors = [e.get("message") for e in reply.get("errors", []) if isinstance(e, dict)]
        if detail := "; ".join(m for m in errors if m) or reply.get("message"):
            return str(detail)
    except ValueError, AttributeError, TypeError:
        pass
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit code {result.returncode}"


def pr_state(repo_dir: Path, url: str) -> str | None:
    """`OPEN`, `MERGED` or `CLOSED`, or None if `gh` can't tell us (not installed, not signed
    in, offline).
    """
    try:
        return json.loads(gh(repo_dir, ["pr", "view", url, "--json", "state"], timeout=30))["state"]
    except GhError, ValueError, KeyError, TypeError:
        return None


def default_branch(repo_dir: Path) -> str | None:
    """The remote's default branch if there is a remote (`origin/main`), else a local `main` or
    `master`.
    """
    try:
        ref = git(repo_dir, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
        if ref.strip():
            return ref.strip()
    except GitError:
        pass
    for name in ("origin/main", "origin/master", "main", "master"):
        try:
            if rev_parse(repo_dir, name) is not None:
                return name
        except GitError:
            return None
    return None


# --- the base branch ---


@dataclass(frozen=True, slots=True)
class Base:
    """Where an implementer starts its branch, and what its pull request targets."""

    branch: str | None
    """A branch name without `origin/`. None if the code repo has no branch we can find."""
    stacked_on: NodeId | None = None
    """The dependency whose unmerged branch this is; None when it's the default branch."""
    unmerged: tuple[tuple[NodeId, str], ...] = ()
    """Set when several dependencies have unmerged branches: a branch can only stack on one, so
    the work starts from the default branch and has to wait for them.
    """


def choose_base(project: Project, node_id: NodeId, *, repo_key: str | None = None) -> Base:
    """The base for implementing `node_id`.

    The candidates are the delivery branches of what the node depends on: its own dependencies
    and its ancestors', and for a dependency with children, their leaves'. A candidate is *open*
    if its branch still exists (on `origin`, else locally) and isn't merged: its pull request
    isn't merged, and its tip isn't on the default branch. `origin` is fetched first.

    - Exactly one open candidate: stack on it.
    - None: the default branch.
    - Several: the default branch, with the open ones listed so the agent can say what it waits on.
    """
    graph = load_graph(project)
    config = read_config(project.apm_dir)
    key = repo_key or default_repo(config)
    repo = config.code.repos.get(key)
    if repo is None:
        return Base(None)
    repo_dir = (project.apm_dir / repo.path).resolve()
    try:
        git(repo_dir, ["rev-parse", "--git-dir"])
    except GitError:
        return Base(None)
    _fetch(repo_dir)
    target = default_branch(repo_dir)
    if target is None:
        return Base(None)
    open_branches: list[tuple[NodeId, str]] = []
    seen: set[str] = set()
    for dep, branch, pr in _dependency_branches(graph, node_id, key):
        if branch not in seen:
            seen.add(branch)
            if _is_open(repo_dir, branch, pr, target):
                open_branches.append((dep, branch))
    if len(open_branches) == 1:
        (dep, branch), *_ = open_branches
        return Base(branch, stacked_on=dep)
    name = target.removeprefix("origin/")
    return Base(name, unmerged=tuple(open_branches) if open_branches else ())


def _dependency_branches(
    graph: Graph, node_id: NodeId, repo_key: str
) -> Iterator[tuple[NodeId, str, str | None]]:
    """(leaf, branch, pr) for every delivered leaf the node depends on, nearest first."""
    deps = dict.fromkeys(
        dep for owner in (node_id, *graph.ancestors(node_id)) for dep in graph.dependencies(owner)
    )
    for dep in deps:
        for leaf in graph.leaves(dep):
            node = graph[leaf]
            delivery = node.delivery
            if node.status is Status.DROPPED or delivery is None or not delivery.branch:
                continue
            if delivery.repo == repo_key:
                yield leaf, delivery.branch, delivery.pr


def _is_open(repo_dir: Path, branch: str, pr: str | None, target: str) -> bool:
    """True if `branch` still exists and its work isn't on `target` yet."""
    if pr and pr_state(repo_dir, pr) == "MERGED":
        return False
    tip = _tip(repo_dir, branch)
    if tip is None:
        return False  # gone (deleted once merged, or never pushed): nothing to stack on
    try:
        return not is_ancestor(repo_dir, tip, target)
    except GitError:
        return False


def _tip(repo_dir: Path, branch: str) -> str | None:
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        try:
            if sha := rev_parse(repo_dir, ref):
                return sha
        except GitError:
            continue
    return None


def _fetch(repo_dir: Path) -> None:
    """`git fetch --prune origin`, if there is an `origin`. Best effort: offline is fine."""
    try:
        git(repo_dir, ["remote", "get-url", "origin"])
    except GitError:
        return
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "--quiet", "--prune", "origin"],
            capture_output=True,
            timeout=FETCH_TIMEOUT_S,
            check=False,
            env=env,
        )


# --- the pull request ---


_GITHUB_REPO = re.compile(r"[/:](?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")


def github_repo(remote: str | None) -> tuple[str, str] | None:
    """(owner, name) from a remote URL: `https://github.com/o/r`, `git@github.com:o/r.git`, or a
    proxy URL that ends in `/o/r`.
    """
    match = _GITHUB_REPO.search(remote.strip()) if remote else None
    return (match["owner"], match["name"]) if match else None


@dataclass(frozen=True, slots=True)
class PrOutcome:
    """What `ensure_pull_request` found or did."""

    pr: str | None = None
    action: Literal["already", "found", "opened"] | None = None
    """`already`: the agent recorded one. `found`: an open one existed for the branch and is now
    recorded. `opened`: APM opened it. None: there is no pull request, and `problem` says why.
    """
    problem: str | None = None
    branch: str | None = None
    base: str | None = None


def ensure_pull_request(project: Project, node_id: NodeId, *, run: str | None = None) -> PrOutcome:
    """Makes sure a delivered leaf has a pull request, and that it's recorded on the node.

    If the delivery has a branch but no pull request, it looks for an open one for that branch,
    and opens one if there is none: title the node's title, body the node's body and its
    `APM-Node` line, base from `choose_base`. Nothing is marked done either way.
    """
    graph = load_graph(project)
    if node_id not in graph:
        return PrOutcome(problem="the node no longer exists")
    delivery = graph[node_id].delivery
    if delivery is not None and delivery.pr:
        return PrOutcome(pr=delivery.pr, action="already", branch=delivery.branch)
    if delivery is None or not delivery.branch:
        return PrOutcome(problem="the run recorded no branch, so there is nothing to open one from")
    branch = delivery.branch
    config = read_config(project.apm_dir)
    repo = config.code.repos.get(delivery.repo)
    if repo is None:
        return PrOutcome(problem=f'repo "{delivery.repo}" is not in apm.yaml', branch=branch)
    repo_dir = (project.apm_dir / repo.path).resolve()
    base = choose_base(project, node_id, repo_key=delivery.repo).branch
    if base is None:
        return PrOutcome(
            problem="the code repo has no default branch to open it against", branch=branch
        )
    remote = repo.remote or _origin_url(repo_dir)
    slug = github_repo(remote)
    if slug is None:
        what = f"`{remote}`" if remote else "the code repo (it has no `origin`)"
        return PrOutcome(
            problem=f"couldn't tell which GitHub repository {what} is", branch=branch, base=base
        )
    owner, name = slug
    title = graph[node_id].title
    body = f"{read_body(project.apm_dir, node_id).strip()}\n\nAPM-Node: {node_id}\n".lstrip()
    try:
        url = _open_pr_for(repo_dir, owner, name, branch)
        action: Literal["found", "opened"] = "found"
        if url is None:
            url = _create_pr(repo_dir, owner, name, title=title, head=branch, base=base, body=body)
            action = "opened"
    except GhError as err:
        return PrOutcome(problem=str(err), branch=branch, base=base)
    attach_delivery(project, node_id, pr=url, actor=ACTOR, run=run)
    return PrOutcome(pr=url, action=action, branch=branch, base=base)


def _origin_url(repo_dir: Path) -> str | None:
    try:
        return git(repo_dir, ["remote", "get-url", "origin"]).strip() or None
    except GitError:
        return None


def _open_pr_for(repo_dir: Path, owner: str, name: str, branch: str) -> str | None:
    """The URL of an open pull request from `branch`, if there is one."""
    query = ["-f", f"head={owner}:{branch}", "-f", "state=open"]
    reply = gh(repo_dir, ["api", "-X", "GET", f"repos/{owner}/{name}/pulls", *query])
    try:
        pulls = json.loads(reply)
        return str(pulls[0]["html_url"]) if pulls else None
    except ValueError, KeyError, IndexError, TypeError:
        raise GhError("`gh api` gave an answer APM couldn't read") from None


def _create_pr(
    repo_dir: Path, owner: str, name: str, *, title: str, head: str, base: str, body: str
) -> str:
    fields = {"title": title, "head": head, "base": base, "body": body}
    args = ["api", "-X", "POST", f"repos/{owner}/{name}/pulls"]
    for key, value in fields.items():
        args += ["-f", f"{key}={value}"]
    reply = gh(repo_dir, args)
    try:
        return str(json.loads(reply)["html_url"])
    except ValueError, KeyError, TypeError:
        raise GhError("`gh api` gave an answer APM couldn't read") from None


def note_missing_pr(
    project: Project, node_id: NodeId, outcome: PrOutcome, *, run: str | None
) -> None:
    """Says under the node's Notes that its work has no pull request, and why. Once per reason."""
    where = f" against `{outcome.base}`" if outcome.base else ""
    line = (
        f"**No pull request yet.** Branch `{outcome.branch}` is delivered, but APM couldn't open"
        f" a pull request for it{where}: {outcome.problem}."
    )
    body = read_body(project.apm_dir, node_id)
    if line not in body:
        edit_body(project, node_id, add_note(body, line), actor=ACTOR, run=run)


def add_note(body: str, line: str) -> str:
    """`body` with `line` added at the end of its `## Notes` section (created if missing)."""
    lines = body.rstrip("\n").split("\n") if body.strip() else []
    start = next((i for i, text in enumerate(lines) if text.strip() == "## Notes"), None)
    if start is None:
        return "\n".join([*lines, "", "## Notes", "", line]).lstrip("\n") + "\n"
    later = (i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    end = next(later, len(lines))
    before, section, after = lines[: start + 1], lines[start + 1 : end], lines[end:]
    while section and not section[-1].strip():
        section.pop()
    section = [*section, "", line] if any(s.strip() for s in section) else ["", line]
    return "\n".join([*before, *section, *(["", *after] if after else [])]) + "\n"
