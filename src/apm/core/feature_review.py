"""Reviewing a finished feature (plans.md §7.6).

A feature is a node with children. Its tasks go into the feature branch, and its review step
(`apm.core.review_steps`) waits on all of them. Once they are done, the feature reviewer, an
agent, reads the feature branch and the feature's description and says whether the feature
really does what the description asks. This module is the plain code around it:

- `find_target`: which branch and commit to review: the feature branch, recorded on the review
  step. For a plan from before review steps, or when you name one, another branch: the one the
  tasks' pull requests merged into, the top of their stack, or the default branch.
- `prepare_worktree`: a checkout of that commit, so the agent reads the feature as built and your
  own checkout is never touched.
- `record_review`: the verdict goes into the review step's description under `## Feature review`,
  and the gaps the reviewer found become a proposal of follow-up tasks on the feature.
- `conclude`: a complete feature's branch gets its pull request, to the enclosing feature's
  branch or the default branch, and the review step waits for it to merge (`apm reconcile` then
  marks the step and the feature done). An incomplete one's step goes back to do.
"""

import contextlib
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apm.core.config import default_repo, read_config
from apm.core.delivery import (
    PrOutcome,
    PullRequest,
    branch_is_open,
    branch_tip,
    default_branch,
    ensure_pull_request,
    feature_branch,
    fetch_origin,
    read_pr,
)
from apm.core.errors import ApmError, format_issue
from apm.core.files import read_body
from apm.core.git import GitError, git, is_ancestor
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import Delivery, NodeId, Status
from apm.core.ops import attach_delivery, edit_body, set_status
from apm.core.project import Project, load_graph
from apm.core.proposals import DECOMPOSITION_SCHEMA, Decomposition, ProposedChild
from apm.core.store import Proposal, Store

AGENT = "apm-feature-reviewer"
ACTOR = f"agent:{AGENT}"
SECTION = "Feature review"
WORKTREE_PREFIX = "apm-review-"

NonEmpty = Annotated[str, Field(min_length=1)]


class ReviewRefused(ApmError, ValueError):  # noqa: N818 (a refusal, not a failure)
    """There's nothing to review, or APM can't tell which branch holds the feature."""


# --- what the reviewer returns ---


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class CriterionCheck(_Model):
    criterion: NonEmpty
    status: Literal["met", "partial", "missing", "unverified"]
    evidence: str = ""
    """Where it is shown: files and lines, a test, a command that was run."""


class FeatureIssue(_Model):
    severity: Literal["blocker", "major", "minor"] = "minor"
    problem: NonEmpty
    where: str = ""
    suggestion: str = ""


class FeatureReview(_Model):
    verdict: Literal["complete", "incomplete"]
    summary: str = ""
    criteria: list[CriterionCheck] = Field(default_factory=list)
    issues: list[FeatureIssue] = Field(default_factory=list)
    follow_ups: list[ProposedChild] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        """The verdict, held to its own evidence: no criterion missing or partly met, and no
        blocker or major issue. A reviewer that says "complete" despite those is overruled.
        """
        return (
            self.verdict == "complete"
            and all(c.status in ("met", "unverified") for c in self.criteria)
            and not any(i.severity in ("blocker", "major") for i in self.issues)
        )


_CHILD = DECOMPOSITION_SCHEMA["properties"]["children"]["items"]

# Written out by hand, like the other schemas: this is exactly what `claude --json-schema` gets.
FEATURE_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "criteria", "issues", "follow_ups"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["complete", "incomplete"],
            "description": "complete only if every criterion is met and nothing major is wrong.",
        },
        "summary": {"type": "string", "description": "The verdict and why, in a few sentences."},
        "criteria": {
            "type": "array",
            "description": "Every acceptance criterion of the feature, in its order.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["criterion", "status", "evidence"],
                "properties": {
                    "criterion": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["met", "partial", "missing", "unverified"],
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Files and lines, tests, or commands that show it; or"
                        " what is missing.",
                    },
                },
            },
        },
        "issues": {
            "type": "array",
            "description": "Problems beyond the criteria: decisions not honoured, broken"
            " integration between tasks, missing tests or docs, bugs.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "problem", "where", "suggestion"],
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "problem": {"type": "string"},
                    "where": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
            },
        },
        "follow_ups": {
            "type": "array",
            "description": "New tasks that would close the gaps, each small enough for one pull"
            " request. Empty when the feature is complete.",
            "items": _CHILD,
        },
    },
}


def parse_review(data: object) -> FeatureReview:
    try:
        return FeatureReview.model_validate(data)
    except ValidationError as err:
        problems = "; ".join(format_issue(e) for e in err.errors())
        raise ReviewRefused(f"not a valid feature review: {problems}") from None


# --- what to review ---


@dataclass(frozen=True, slots=True)
class Task:
    """One of the feature's leaves, and whether its work is in the commit under review."""

    node_id: NodeId
    status: Status
    branch: str | None
    pr: str | None
    included: bool | None
    """True if its work is in the reviewed commit, False if not, None if APM can't tell."""


@dataclass(frozen=True, slots=True)
class Target:
    branch: str
    """Without `origin/`."""
    sha: str
    default: str | None
    """The default branch, without `origin/`: what the feature is compared with."""
    why: str
    """How APM picked the branch, for the conversation."""
    tasks: tuple[Task, ...]
    feature: NodeId
    """The node whose description is the spec."""
    step: NodeId | None
    """Its review step, if it has one (plans from before review steps don't)."""

    @property
    def short(self) -> str:
        return self.sha[:7]

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> Target:
        """The target a review's first turn recorded (without its task list)."""
        return cls(
            branch=str(event["branch"]),
            sha=str(event["sha"]),
            default=event.get("default"),
            why="",
            tasks=(),
            feature=str(event.get("feature") or event.get("step") or ""),
            step=event.get("step"),
        )

    def as_event(self) -> dict[str, Any]:
        return {
            "type": "apm_review_target",
            "branch": self.branch,
            "sha": self.sha,
            "default": self.default,
            "feature": self.feature,
            "step": self.step,
        }


def review_subject(graph: Graph, node_id: NodeId) -> tuple[NodeId, NodeId | None]:
    """(feature, its review step) for a review started on `node_id`: a review step, or a feature
    (which is mapped to its step), or a node without one (reviewed as it is).
    """
    if graph.is_review(node_id) and (parent := graph[node_id].parent) is not None:
        return parent, node_id
    return node_id, graph.review_step(node_id)


def check_reviewable(graph: Graph, node_id: NodeId, branch: str | None) -> None:
    """Refuses at once, without touching git, when there is plainly nothing to review: a review
    step whose tasks aren't all done, or nothing delivered at all.
    """
    feature, step = review_subject(graph, node_id)
    if step is not None:
        if graph[step].status is Status.DROPPED:
            raise ReviewRefused("this feature's review step is dropped")
        waiting = [
            c
            for c in graph.children(feature)
            if c != step and graph[c].status not in (Status.DONE, Status.DROPPED)
        ]
        if waiting:
            names = ", ".join(handle(c) for c in waiting)
            raise ReviewRefused(f"not ready: the review waits until every task is done ({names})")
    if branch and branch.strip():
        return
    if step is not None and graph[step].delivery is not None:
        return  # the feature branch is recorded
    if not any(_delivery(graph, leaf) for leaf in _tasks(graph, feature)):
        raise ReviewRefused(
            "nothing under this node has been delivered yet, so APM can't tell which branch"
            " holds the feature: name the branch to review"
        )


def find_target(project: Project, node_id: NodeId, branch: str | None = None) -> Target:
    """The branch and commit to review for `node_id`. Fetches `origin` first.

    `branch` wins if you name it. Otherwise, from the tasks' deliveries:

    1. The tasks' pull requests merged into one branch other than the default: that is the
       feature branch.
    2. Some of their work isn't merged anywhere yet: the branch that holds all of it, if one
       does (one open branch, or the top of their stack).
    3. All of it is on the default branch: review the default branch.

    Anything else is ambiguous, and you are asked to name the branch.
    """
    graph = load_graph(project)
    feature, step = review_subject(graph, node_id)
    repo_dir = _repo_dir(project)
    fetch_origin(repo_dir)
    default_ref = default_branch(repo_dir)
    default = default_ref.removeprefix("origin/") if default_ref else None
    tasks = _tasks(graph, feature)
    prs = {
        leaf: read_pr(repo_dir, delivery.pr)
        for leaf in tasks
        if (delivery := _delivery(graph, leaf)) is not None and delivery.pr
    }
    recorded = feature_branch(graph, feature) if step is not None else None
    if branch and branch.strip():
        name, why = branch.strip().removeprefix("origin/"), "you named it"
    elif recorded is not None and branch_tip(repo_dir, recorded) is not None:
        name, why = recorded, "it is the feature branch"
    else:
        name, why = _infer(graph, repo_dir, tasks, prs, default, default_ref)
    sha = branch_tip(repo_dir, name)
    if sha is None:
        raise ReviewRefused(f"there is no branch {name!r} here or on origin")
    return Target(
        branch=name,
        sha=sha,
        default=default,
        why=why,
        tasks=tuple(_task(graph, repo_dir, leaf, prs.get(leaf), name, sha) for leaf in tasks),
        feature=feature,
        step=step,
    )


def _infer(
    graph: Graph,
    repo_dir: Path,
    tasks: list[NodeId],
    prs: dict[NodeId, PullRequest | None],
    default: str | None,
    default_ref: str | None,
) -> tuple[str, str]:
    delivered = [(leaf, d) for leaf in tasks if (d := _delivery(graph, leaf)) is not None]
    if not delivered:
        raise ReviewRefused(
            "nothing under this node has been delivered yet, so APM can't tell which branch"
            " holds the feature: name the branch to review"
        )
    merged_into = {pr.base for pr in prs.values() if pr is not None and pr.merged}
    features = sorted(merged_into - {default})
    if len(features) == 1:
        return features[0], "the tasks' pull requests merged into it"

    open_branches = [
        (leaf, d.branch)
        for leaf, d in delivered
        if d.branch and default_ref and branch_is_open(repo_dir, d.branch, d.pr, default_ref)
    ]
    if len(open_branches) == 1:
        return open_branches[0][1], "it holds the tasks' unmerged work"
    if open_branches:
        top = graph.stack_top(leaf for leaf, _ in open_branches)
        if top is not None:
            return dict(open_branches)[top], "it is the top of the tasks' stack"
        names = ", ".join(sorted({b for _, b in open_branches}))
        raise ReviewRefused(
            f"the tasks' unmerged work is on separate branches ({names}): name the branch that"
            " holds the whole feature"
        )
    if len(features) > 1:
        raise ReviewRefused(
            f"the tasks' pull requests merged into different branches ({', '.join(features)}):"
            " name the branch that holds the whole feature"
        )
    if default is None:
        raise ReviewRefused("the code repo has no default branch: name the branch to review")
    return default, "all of the tasks' work is on it"


def _task(
    graph: Graph, repo_dir: Path, leaf: NodeId, pr: PullRequest | None, branch: str, sha: str
) -> Task:
    delivery = _delivery(graph, leaf)
    node = graph[leaf]
    if delivery is None:
        return Task(leaf, node.status, None, None, included=False)
    included: bool | None = None
    candidates = [*delivery.commits]
    if pr is not None and pr.merged and pr.merge_commit:
        candidates.append(pr.merge_commit)
    if delivery.branch and (tip := branch_tip(repo_dir, delivery.branch)):
        candidates.append(tip)
    with contextlib.suppress(GitError):
        if any(_contains(repo_dir, sha, commit) for commit in candidates):
            included = True
        elif pr is not None and pr.merged and pr.base == branch:
            included = True  # squash-merged into it: the PR says so even if the SHAs don't
        elif candidates:
            included = False
    return Task(leaf, node.status, delivery.branch, delivery.pr, included)


def _contains(repo_dir: Path, sha: str, commit: str) -> bool:
    try:
        return is_ancestor(repo_dir, commit, sha)
    except GitError:
        return False  # a commit this clone doesn't have


def _tasks(graph: Graph, node_id: NodeId) -> list[NodeId]:
    """The feature's work: its leaves that aren't dropped or review steps (the node itself, if it
    is a leaf).
    """
    return [
        leaf
        for leaf in graph.leaves(node_id)
        if graph[leaf].status is not Status.DROPPED and not graph.is_review(leaf)
    ]


def _delivery(graph: Graph, leaf: NodeId) -> Delivery | None:
    delivery = graph[leaf].delivery
    return delivery if delivery is not None and (delivery.branch or delivery.pr) else None


def _repo_dir(project: Project) -> Path:
    config = read_config(project.apm_dir)
    repo = config.code.repos[default_repo(config)]
    repo_dir = (project.apm_dir / repo.path).resolve()
    try:
        git(repo_dir, ["rev-parse", "--git-dir"])
    except GitError:
        raise ReviewRefused(f"{repo_dir} is not a git repository") from None
    return repo_dir


# --- where the reviewer works ---


def worktree_path(project: Project, node_id: NodeId) -> Path:
    """One per feature: a new review replaces the last one's checkout."""
    return _repo_dir(project) / ".claude" / "worktrees" / f"{WORKTREE_PREFIX}{handle(node_id)}"


def prepare_worktree(project: Project, node_id: NodeId, target: Target) -> Path:
    """A detached checkout of the commit under review, at the same place for every review of this
    feature, so a reply to an earlier review still finds its conversation.
    """
    repo_dir = _repo_dir(project)
    path = worktree_path(project, node_id)
    if path.exists():
        with contextlib.suppress(GitError):
            git(repo_dir, ["worktree", "remove", "--force", "--force", str(path)])
        shutil.rmtree(path, ignore_errors=True)
    with contextlib.suppress(GitError):
        git(repo_dir, ["worktree", "prune"])
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo_dir, ["worktree", "add", "--quiet", "--detach", str(path), target.sha])
    return path


# --- recording the verdict ---


@dataclass(frozen=True, slots=True)
class Recorded:
    complete: bool
    proposal: Proposal | None
    replaced: tuple[str, ...]
    """Pending follow-up proposals from earlier turns of the same review, now rejected."""


def record_review(
    project: Project,
    store: Store,
    node_id: NodeId,
    review: FeatureReview,
    *,
    branch: str,
    sha: str,
    run: str | None = None,
    supersedes: Iterable[str] = (),
    feature: NodeId | None = None,
) -> Recorded:
    """Writes the verdict into `node_id`'s description (the review step's), under
    `## Feature review` (replacing the last one), and turns the follow-ups into a proposal on
    `feature` (default: `node_id`), where accepting them adds tasks. A newer turn of the same
    review replaces its earlier, still pending, proposal.
    """
    where = feature or node_id
    body = read_body(project.apm_dir, node_id)
    section = render_section(review, branch=branch, sha=sha)
    updated = replace_section(body, SECTION, section)
    if updated != body:
        edit_body(project, node_id, updated, actor=ACTOR, run=run)

    earlier = set(supersedes)
    replaced = []
    for pending in store.list_proposals(node_id=where, status="pending"):
        if pending.run_id in earlier:
            store.resolve_proposal(pending.id, "rejected")
            replaced.append(pending.id)

    proposal = None
    if review.follow_ups:
        data = Decomposition(
            rationale=f"Follow-ups from the feature review of {branch} at {sha[:7]}."
            + (f" {review.summary}" if review.summary else ""),
            children=review.follow_ups,
            warnings=[_issue_line(issue) for issue in review.issues],
        ).model_dump(mode="json")
        proposal = store.create_proposal(node_id=where, data=data, run_id=run)
    return Recorded(review.complete, proposal, tuple(replaced))


@dataclass(frozen=True, slots=True)
class Concluded:
    status: Status | None
    """The review step's status now, or None if there is no step."""
    pr: PrOutcome | None = None
    landed: bool = False
    """True if the reviewed branch is already the feature's destination: nothing to open."""


def conclude(
    project: Project, target: Target, review: FeatureReview, *, run: str | None = None
) -> Concluded:
    """Moves the review step on after a verdict.

    - Incomplete: the step goes back to do; the follow-ups (if accepted) are new tasks it waits
      on, and you run the review again once they're done.
    - Complete: the feature branch gets its pull request into its home (the enclosing feature's
      branch, or the default branch), and the step stays in progress until it merges. If the
      reviewed branch already is that home, the step is done.
    """
    step = target.step
    if step is None:
        return Concluded(None)
    graph = load_graph(project)
    if not review.complete:
        if graph[step].status is not Status.TODO:
            set_status(
                project,
                step,
                Status.TODO,
                note="The feature review found gaps.",
                actor=ACTOR,
                run=run,
            )
        return Concluded(Status.TODO)
    home = graph.feature_of(step)
    destination = feature_branch(graph, home) if home is not None else target.default
    if target.branch == destination:
        set_status(
            project,
            step,
            Status.DONE,
            note=f"The feature review passed on {target.branch}, where the feature belongs.",
            actor=ACTOR,
            run=run,
        )
        graph = load_graph(project)
        children = graph.children(target.feature)
        if graph[target.feature].status not in (Status.DONE, Status.DROPPED) and all(
            graph[c].status in (Status.DONE, Status.DROPPED) for c in children
        ):
            set_status(project, target.feature, Status.DONE, actor=ACTOR, run=run)
        return Concluded(Status.DONE, landed=True)
    # The reviewed commit is recorded too: without a pull request, it is how `apm reconcile`
    # tells that the feature branch has landed.
    delivery = graph[step].delivery
    if delivery is None or delivery.branch != target.branch or target.sha not in delivery.commits:
        attach_delivery(
            project, step, branch=target.branch, commits=[target.sha], actor=ACTOR, run=run
        )
    if graph[step].status is not Status.IN_PROGRESS:
        set_status(project, step, Status.IN_PROGRESS, actor=ACTOR, run=run)
    feature = graph[target.feature]
    body = (
        f"{read_body(project.apm_dir, target.feature).strip()}\n\n"
        f"{read_body(project.apm_dir, step).strip()}"
    )
    outcome = ensure_pull_request(project, step, run=run, title=feature.title, body=body)
    return Concluded(Status.IN_PROGRESS, pr=outcome)


_STATUS = {"met": "Met", "partial": "Partly", "missing": "Missing", "unverified": "Not checked"}


def render_section(
    review: FeatureReview, *, branch: str, sha: str, when: datetime | None = None
) -> str:
    """The `## Feature review` section, in markdown."""
    day = (when or datetime.now(UTC)).date().isoformat()
    verdict = "Complete" if review.complete else "Incomplete"
    lines = [f"## {SECTION}", "", f"**{verdict}** · `{branch}` at `{sha[:7]}` · {day}"]
    if review.verdict == "complete" and not review.complete:
        lines += [
            "",
            "_The reviewer said complete, but a criterion isn't met or a major issue is open._",
        ]
    if review.summary:
        lines += ["", review.summary.strip()]
    if review.criteria:
        lines += ["", "| | Acceptance criterion | Evidence |", "|---|---|---|"]
        lines += [
            f"| {_STATUS[c.status]} | {_cell(c.criterion)} | {_cell(c.evidence)} |"
            for c in review.criteria
        ]
    if review.issues:
        lines += ["", "**Issues**", ""]
        lines += [f"- {_issue_line(issue)}" for issue in review.issues]
    if review.follow_ups:
        count = len(review.follow_ups)
        tasks = "task" if count == 1 else "tasks"
        lines += ["", f"{count} follow-up {tasks} proposed: accept them under Proposals."]
    return "\n".join(lines) + "\n"


def _issue_line(issue: FeatureIssue) -> str:
    text = f"**{issue.severity}**: {issue.problem.strip()}"
    if issue.where:
        text += f" ({issue.where.strip()})"
    if issue.suggestion:
        text += f" Suggestion: {issue.suggestion.strip()}"
    return text


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|") or "—"


def replace_section(body: str, title: str, section: str) -> str:
    """`body` with its `## <title>` section replaced by `section`, or `section` added at the end.
    Every other section is left exactly as it was.
    """
    lines = body.rstrip("\n").split("\n") if body.strip() else []
    heading = f"## {title}"
    start = next((i for i, text in enumerate(lines) if text.strip() == heading), None)
    new = section.rstrip("\n").split("\n")
    if start is None:
        return "\n".join([*lines, "", *new] if lines else new) + "\n"
    later = (i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    end = next(later, len(lines))
    after = lines[end:]
    return "\n".join([*lines[:start], *new, *(["", *after] if after else [])]) + "\n"
