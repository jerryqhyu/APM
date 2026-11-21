"""Reconciling the plan with the code repo (plans.md §7.5): a node is done once its work is on the
default branch.

This is plain code, not an agent: the question "did it merge?" has a definite answer.

- A node with a pull request is done when the PR is merged into the default branch, or into a
  branch that has since landed there. A PR merged into another branch (a stack) leaves the node in
  progress, with a note saying where its work is.
- Otherwise it is done when its delivery commits are on the default branch: the commits themselves,
  or, after a squash or a rebase, the same changes (compared by patch id).
- An open PR that still targets a branch which has landed is reported, and with `retarget=True`
  pointed at the default branch.

Pull requests are read through the GitHub REST API (`gh api`), which works where `gh pr`'s GraphQL
calls are refused. Without `gh`, reconcile falls back to what local git can tell.
"""

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from apm.core.config import read_config
from apm.core.files import read_body
from apm.core.git import GitError, git, is_ancestor, rev_parse
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import Delivery, NodeId, Status
from apm.core.ops import edit_body, set_status
from apm.core.project import Project, load_graph

GH_TIMEOUT_S = 30.0
FETCH_TIMEOUT_S = 30.0
PATCH_ID_SCAN = 500
"""How many recent commits of the default branch a squash merge is looked for in."""


@dataclass(frozen=True, slots=True)
class Reconciled:
    node_id: NodeId
    title: str
    reason: str
    """Why it was marked done, e.g. "PR #20 merged into main"."""


@dataclass(frozen=True, slots=True)
class Finding:
    """Something reconcile noticed about a node and didn't mark done."""

    node_id: NodeId
    title: str
    kind: FindingKind
    message: str


@dataclass(frozen=True, slots=True)
class Report:
    done: list[Reconciled] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def reconcile(project: Project, *, actor: str = "user", retarget: bool = False) -> Report:
    """Marks every in-progress leaf whose work is on the default branch as done, one commit per
    node, and reports what else it noticed. With `retarget`, an open PR whose base has landed is
    pointed at the default branch.
    """
    graph = load_graph(project)
    config = read_config(project.apm_dir)
    report = Report()
    fetched: set[Path] = set()
    for node_id in sorted(graph.nodes):
        node = graph[node_id]
        if node.status is not Status.IN_PROGRESS or node.delivery is None:
            continue
        repo = config.code.repos.get(node.delivery.repo)
        if repo is None:
            continue
        repo_dir = (project.apm_dir / repo.path).resolve()
        if repo_dir not in fetched:
            fetched.add(repo_dir)
            _fetch(repo_dir)
        judge = _Judge(project, graph, repo_dir, node_id, node.delivery, actor, retarget)
        if (reason := judge.done_reason()) is not None:
            set_status(project, node_id, Status.DONE, note=f"Reconciled: {reason}.", actor=actor)
            report.done.append(Reconciled(node_id, node.title, reason))
        report.findings.extend(judge.findings)
    return report


def describe(report: Report) -> list[str]:
    lines = [f"{handle(r.node_id)} {r.title}: done ({r.reason})" for r in report.done]
    lines += [f"{handle(f.node_id)} {f.title}: {f.message}" for f in report.findings]
    return lines


# --- judging one node ---


class _Judge:
    def __init__(
        self,
        project: Project,
        graph: Graph,
        repo_dir: Path,
        node_id: NodeId,
        delivery: Delivery,
        actor: str,
        retarget: bool,
    ) -> None:
        self.project, self.graph, self.repo_dir, self.node_id = project, graph, repo_dir, node_id
        self.delivery, self.actor, self.retarget = delivery, actor, retarget
        self.target = default_branch(repo_dir)
        self.findings: list[Finding] = []

    @property
    def target_name(self) -> str | None:
        return self.target.removeprefix("origin/") if self.target else None

    def done_reason(self) -> str | None:
        if self.delivery.pr and (pr := read_pr(self.repo_dir, self.delivery.pr)) is not None:
            return self._judge_pr(pr)
        return self._judge_commits()

    def _judge_pr(self, pr: PullRequest) -> str | None:
        target = self.target_name
        if pr.merged:
            if target is None or pr.base == target:
                return f"PR #{pr.number} merged into {pr.base}"
            if self._on_target(pr.merge_commit):
                return f"PR #{pr.number} merged into {pr.base}, which is now on {target}"
            owner = self._owner_of(pr.base)
            where = f"{pr.base} ({owner}'s branch)" if owner else pr.base
            message = f"PR #{pr.number} merged into {where}, not yet on {target}"
            self._note(message, "merged-elsewhere")
            return None
        if pr.state == "closed":
            self._find(f"PR #{pr.number} was closed without merging", "closed")
            return None
        if target is not None and pr.base != target and self._landed(pr.base):
            self._retarget(pr, target)
        return None

    def _judge_commits(self) -> str | None:
        commits, target = self.delivery.commits, self.target
        if not commits or target is None:
            return None
        name = target.removeprefix("origin/")
        try:
            if all(is_ancestor(self.repo_dir, commit, target) for commit in commits):
                return f"its commits are on {name}"
        except GitError:  # a commit this clone doesn't have
            return None
        if _same_changes_on(self.repo_dir, commits, target):
            return f"its changes are on {name} (squashed or rebased)"
        return None

    # --- helpers ---

    def _on_target(self, sha: str | None) -> bool:
        if not sha or self.target is None:
            return False
        try:
            return is_ancestor(self.repo_dir, sha, self.target)
        except GitError:
            return False

    def _owner_of(self, branch: str) -> str | None:
        """How to name the node that delivered `branch`: its PR number, else its handle."""
        for other in self.graph.nodes.values():
            d = other.delivery
            if other.id != self.node_id and d is not None and d.branch == branch:
                number = pr_number(d.pr) if d.pr else None
                return f"#{number}" if number else handle(other.id)
        return None

    def _landed(self, branch: str) -> bool:
        """True if `branch`'s work is on the default branch: its tip is, or the node that
        delivered it is done, or that node's PR merged into the default branch.
        """
        tip = _tip(self.repo_dir, branch)
        if tip is not None and self._on_target(tip):
            return True
        for other in self.graph.nodes.values():
            d = other.delivery
            if other.id == self.node_id or d is None or d.branch != branch:
                continue
            if other.status is Status.DONE:
                return True
            pr = read_pr(self.repo_dir, d.pr) if d.pr else None
            if pr and pr.merged and self._merge_on_target(pr):
                return True
        return False

    def _merge_on_target(self, pr: PullRequest) -> bool:
        return pr.base == self.target_name or self._on_target(pr.merge_commit)

    def _retarget(self, pr: PullRequest, target: str) -> None:
        owner = self._owner_of(pr.base)
        where = f"{pr.base} ({owner}'s branch)" if owner else pr.base
        if not self.retarget:
            message = (
                f"PR #{pr.number} still targets {where}, which has landed: retarget it to {target}"
            )
            self._find(message, "retarget")
            return
        try:
            set_pr_base(self.repo_dir, pr, target)
        except GhError as err:
            self._find(f"PR #{pr.number} couldn't be retargeted to {target}: {err}", "retarget")
            return
        self._find(f"PR #{pr.number} retargeted from {where} to {target}", "retargeted")

    def _find(self, message: str, kind: FindingKind) -> None:
        self.findings.append(Finding(self.node_id, self.graph[self.node_id].title, kind, message))

    def _note(self, message: str, kind: FindingKind) -> None:
        """Reports `message`, and writes it under the node's Notes the first time."""
        self._find(message, kind)
        body = read_body(self.project.apm_dir, self.node_id)
        line = f"**Reconcile:** {message}."
        if line not in body:
            edit_body(self.project, self.node_id, add_note(body, line), actor=self.actor)


type FindingKind = Literal["merged-elsewhere", "closed", "retarget", "retargeted"]


# --- pull requests, through the GitHub REST API ---


class GhError(RuntimeError):
    """`gh` isn't there, or it failed."""


def gh(cwd: Path, args: Sequence[str], *, timeout: float = GH_TIMEOUT_S) -> str:
    """Runs `gh <args>` in `cwd` and returns its stdout. Raises `GhError` if it can't."""
    try:
        result = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        raise GhError("`gh` is not installed") from None
    except (OSError, subprocess.TimeoutExpired) as err:
        raise GhError(f"couldn't run `gh`: {err}") from None
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise GhError(lines[-1] if lines else f"`gh` exited with {result.returncode}")
    return result.stdout


_PR_URL = re.compile(r"^https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<n>\d+)")


@dataclass(frozen=True, slots=True)
class PrRef:
    host: str
    owner: str
    repo: str
    number: int

    @property
    def api_path(self) -> str:
        return f"repos/{self.owner}/{self.repo}/pulls/{self.number}"

    @property
    def host_args(self) -> list[str]:
        return [] if self.host == "github.com" else ["--hostname", self.host]


def parse_pr_url(url: str) -> PrRef | None:
    """`https://github.com/o/r/pull/20` → host, owner, repo, number. None if it isn't one."""
    match = _PR_URL.match(url.strip())
    if match is None:
        return None
    return PrRef(match["host"], match["owner"], match["repo"], int(match["n"]))


def pr_number(url: str) -> int | None:
    ref = parse_pr_url(url)
    return ref.number if ref else None


@dataclass(frozen=True, slots=True)
class PullRequest:
    ref: PrRef
    state: str
    """`open` or `closed`, as GitHub says it."""
    merged: bool
    base: str
    """The branch it targets (or targeted when it merged)."""
    merge_commit: str | None

    @property
    def number(self) -> int:
        return self.ref.number


def read_pr(repo_dir: Path, url: str) -> PullRequest | None:
    """The pull request at `url`, or None if it can't be read (not a GitHub PR URL, no `gh`, not
    signed in, offline).
    """
    ref = parse_pr_url(url)
    if ref is None:
        return None
    try:
        data = json.loads(gh(repo_dir, ["api", *ref.host_args, ref.api_path]))
        return PullRequest(
            ref=ref,
            state=str(data["state"]),
            merged=bool(data.get("merged") or data.get("merged_at")),
            base=str(data["base"]["ref"]),
            merge_commit=data.get("merge_commit_sha") or None,
        )
    except GhError, ValueError, KeyError, TypeError:
        return None


def set_pr_base(repo_dir: Path, pr: PullRequest, base: str) -> None:
    """Points `pr` at `base`. Raises `GhError` if GitHub refuses."""
    gh(repo_dir, ["api", *pr.ref.host_args, "-X", "PATCH", pr.ref.api_path, "-f", f"base={base}"])


# --- git ---


def default_branch(repo_dir: Path) -> str | None:
    """The remote's default branch if there is a remote, else a local `main` or `master`."""
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


def _same_changes_on(repo_dir: Path, commits: Sequence[str], target: str) -> bool:
    """True if the changes of `commits` (oldest first) are on `target` under other commits:
    each one rebased (`git cherry`), or all of them squashed into one commit (the patch id of
    their combined diff matches a recent commit on `target`).
    """
    try:
        if all(_cherry_picked(repo_dir, commit, target) for commit in commits):
            return True
        combined = _patch_id(repo_dir, git(repo_dir, ["diff", f"{commits[0]}^", commits[-1]]))
        if combined is None:
            return False
        log = git(
            repo_dir,
            ["log", "-p", f"-{PATCH_ID_SCAN}", "--no-merges", "--format=commit %H", target],
        )
        ids = git(repo_dir, ["patch-id", "--stable"], stdin=log)
        return any(line.split()[0] == combined for line in ids.splitlines() if line.strip())
    except GitError:
        return False


def _cherry_picked(repo_dir: Path, commit: str, target: str) -> bool:
    """True if `target` has a commit with the same change as `commit`."""
    parent = f"{commit}^"
    try:
        rev_parse(repo_dir, parent)
        out = git(repo_dir, ["cherry", target, commit, parent])
    except GitError:
        out = git(repo_dir, ["cherry", target, commit])
    lines = [line for line in out.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("-") for line in lines)


def _patch_id(repo_dir: Path, diff: str) -> str | None:
    if not diff.strip():
        return None
    out = git(repo_dir, ["patch-id", "--stable"], stdin=diff).split()
    return out[0] if out else None


# --- notes ---


def add_note(body: str, line: str) -> str:
    """`body` with `line` added at the end of its `## Notes` section (created if missing)."""
    lines = body.rstrip("\n").split("\n") if body.strip() else []
    start = next((i for i, text in enumerate(lines) if text.strip() == "## Notes"), None)
    if start is None:
        return "\n".join([*lines, "", "## Notes", "", line]).lstrip("\n") + "\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    before, section, after = lines[: start + 1], lines[start + 1 : end], lines[end:]
    while section and not section[-1].strip():
        section.pop()
    section = [*section, "", line] if any(s.strip() for s in section) else ["", line]
    return "\n".join([*before, *section, *(["", *after] if after else [])]) + "\n"
