"""Reconciling the plan with the code repo (plans.md §7.5): a node is done once its work is where
it belongs: its feature's branch for a task inside a feature, else the default branch (a review
step's work, the feature branch, belongs one level up). That is its *home*.

This is plain code, not an agent: the question "did it merge?" has a definite answer.

- A node with a pull request is done when the PR is merged into the default branch, or into a
  branch that has since landed there. A PR merged into another branch (a stack) leaves the node in
  progress, with a note saying where its work is.
- Otherwise it is done when its delivery commits are on the default branch: the commits themselves,
  or, after a squash or a rebase, the same changes (compared by patch id).
- An open PR that still targets a branch which has landed is reported, and with `retarget=True`
  pointed at the default branch.

Pull requests are read through the GitHub REST API (`apm.core.delivery.read_pr`), which works
where `gh pr`'s GraphQL calls are refused. Without `gh`, reconcile falls back to what local git
can tell.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from apm.core.config import read_config
from apm.core.delivery import (
    GhError,
    PullRequest,
    add_note,
    branch_ref,
    branch_tip,
    default_branch,
    fetch_origin,
    home_branch,
    pr_number,
    read_pr,
    set_pr_base,
)
from apm.core.files import read_body
from apm.core.git import GitError, git, is_ancestor, patch_id, rev_parse
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import Delivery, NodeId, Status
from apm.core.ops import edit_body, set_status
from apm.core.project import Project, load_graph

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
            fetch_origin(repo_dir)
        judge = _Judge(project, graph, repo_dir, node_id, node.delivery, actor, retarget)
        if (reason := judge.done_reason()) is not None:
            set_status(project, node_id, Status.DONE, note=f"Reconciled: {reason}.", actor=actor)
            report.done.append(Reconciled(node_id, node.title, reason))
            if graph.is_review(node_id) and (feature := node.parent) is not None:
                _finish_feature(project, feature, actor, report)
        report.findings.extend(judge.findings)
    return report


def _finish_feature(project: Project, feature: NodeId, actor: str, report: Report) -> None:
    """A feature whose review step's pull request has landed is done, once every other child
    is done or dropped too.
    """
    graph = load_graph(project)
    node = graph[feature]
    if node.status in (Status.DONE, Status.DROPPED):
        return
    if all(graph[c].status in (Status.DONE, Status.DROPPED) for c in graph.children(feature)):
        reason = "its review passed and its branch landed"
        set_status(project, feature, Status.DONE, note=f"Reconciled: {reason}.", actor=actor)
        report.done.append(Reconciled(feature, node.title, reason))


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
        home = home_branch(graph, node_id)
        self.target = (branch_ref(repo_dir, home) if home else None) or default_branch(repo_dir)
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
        tip = branch_tip(self.repo_dir, branch)
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


# --- git ---


def _same_changes_on(repo_dir: Path, commits: Sequence[str], target: str) -> bool:
    """True if the changes of `commits` (oldest first) are on `target` under other commits:
    each one rebased (`git cherry`), or all of them squashed into one commit (the patch id of
    their combined diff matches a recent commit on `target`).
    """
    try:
        if all(_cherry_picked(repo_dir, commit, target) for commit in commits):
            return True
        combined = patch_id(repo_dir, f"{commits[0]}^", commits[-1])
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
