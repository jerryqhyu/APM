"""Where a feature report goes, and getting it there (plans.md §7.9).

The report is committed as `docs/features/<slug>-<handle>.md` on the feature's own branch, so it
lands with the feature, and the feature's pull request links it. Plain code around the reporter:

- `check_reportable` / `find_target`: which branch the report goes on, and refusing when there is
  nothing to report on yet. A review step's report is its feature's.
- `prepare_report`: a checkout of the feature branch for the agent to read, and its first
  message, with the record (`apm.core.report`).
- `save_report`: APM itself writes the file, commits it with an `APM-Node` trailer and pushes the
  branch: a plain push, never a force-push. It is kept in the store either way, so the UI can show
  it, with why it couldn't be committed if it couldn't.
- `link_report`: puts a link to the report at the top of the feature's pull request description.
"""

import contextlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm.core.config import ReportDetail, default_repo, read_config
from apm.core.delivery import (
    GhError,
    branch_ref,
    branch_tip,
    default_branch,
    feature_branch,
    fetch_origin,
    gh,
    github_repo,
    home_branch,
    parse_pr_url,
)
from apm.core.git import CommitMessage, GitError, commit, git
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import NodeId, Status
from apm.core.project import Project, load_graph
from apm.core.report import (
    BranchChanges,
    ReportAnswer,
    ReportRefused,
    gather_record,
    render_record,
    report_file,
    report_prompt,
)
from apm.core.settings import feature_settings
from apm.core.store import FeatureReport, Store

REPORT_DIR = "docs/features"
WORKTREE_PREFIX = "apm-report-"
LINK_LABEL = "**Feature report:**"
"""Starts the line in the pull request description that links the report."""


# --- what is reported on, and where it goes ---


def report_subject(graph: Graph, node_id: NodeId) -> NodeId:
    """The feature a report started on `node_id` is about: a review step's is its feature's."""
    parent = graph[node_id].parent
    return parent if graph.is_review(node_id) and parent is not None else node_id


def check_reportable(graph: Graph, node_id: NodeId) -> None:
    """Refuses at once, without touching git, when there is plainly nothing to report on: a node
    with no tasks, or a feature none of whose work is delivered yet.
    """
    if graph.is_leaf(node_id):
        raise ReportRefused("only a feature has a report: this node has nothing inside it")
    step = graph.review_step(node_id)
    if step is not None and graph[step].delivery is not None:
        return  # the feature branch is recorded
    delivered = (graph[leaf].delivery for leaf in graph.leaves(node_id))
    if not any(d is not None and (d.branch or d.pr) for d in delivered):
        raise ReportRefused("nothing is built yet: the feature branch has no work on it")


@dataclass(frozen=True, slots=True)
class ReportTarget:
    feature: NodeId
    branch: str
    """The feature branch, without `origin/`."""
    sha: str
    """Its tip when the report was started: what the agent reads."""
    home: str
    """Where the feature goes: the enclosing feature's branch or the default branch."""
    path: str
    detail: ReportDetail

    @property
    def short(self) -> str:
        return self.sha[:7]

    def as_event(self) -> dict[str, Any]:
        return {
            "type": "apm_report_target",
            "feature": self.feature,
            "branch": self.branch,
            "sha": self.sha,
            "home": self.home,
            "path": self.path,
            "detail": self.detail,
        }

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> ReportTarget:
        return cls(
            feature=str(event["feature"]),
            branch=str(event["branch"]),
            sha=str(event["sha"]),
            home=str(event["home"]),
            path=str(event["path"]),
            detail=event.get("detail") or "standard",
        )


def repo_dir(project: Project) -> Path:
    config = read_config(project.apm_dir)
    path = (project.apm_dir / config.code.repos[default_repo(config)].path).resolve()
    try:
        git(path, ["rev-parse", "--git-dir"])
    except GitError:
        raise ReportRefused(f"{path} is not a git repository") from None
    return path


def find_target(project: Project, feature: NodeId) -> ReportTarget:
    """The feature branch at its tip on `origin` (fetched first), where the feature goes, and the
    report's path. Refuses when the branch doesn't exist or has no work on it.
    """
    graph = load_graph(project)
    repo = repo_dir(project)
    fetch_origin(repo)
    branch = feature_branch(graph, feature)
    sha = branch_tip(repo, branch)
    if sha is None:
        raise ReportRefused(f"the feature has no branch yet ({branch})")
    default = default_branch(repo)
    home = home_branch(graph, feature) or (default or "main").removeprefix("origin/")
    if branch == home or branch == (default or "").removeprefix("origin/"):
        raise ReportRefused(f"the feature has no branch of its own: it is built on {branch}")
    base = branch_ref(repo, home) or default
    if base is not None and int(git(repo, ["rev-list", "--count", f"{base}..{sha}"]) or 0) == 0:
        raise ReportRefused(f"the feature branch {branch} has no work on it yet")
    detail = feature_settings(read_config(project.apm_dir), graph, feature).report_detail
    return ReportTarget(feature, branch, sha, home, report_path(repo, sha, graph, feature), detail)


def report_path(repo: Path, sha: str, graph: Graph, feature: NodeId) -> str:
    """`docs/features/<slug>-<handle>.md`. The handle keeps two features with the same title
    apart, even on branches that can't see each other yet. A file already on the branch for this
    feature keeps its name, so renaming the feature doesn't leave a second report behind.
    """
    suffix = f"-{handle(feature)}.md"
    with contextlib.suppress(GitError):
        listing = git(repo, ["ls-tree", "--name-only", sha, "--", f"{REPORT_DIR}/"])
        for path in listing.splitlines():
            if path.endswith(suffix):
                return path
    words = re.sub(r"[^a-z0-9]+", "-", graph[feature].title.lower()).strip("-")[:48].rstrip("-")
    return f"{REPORT_DIR}/{words or 'feature'}{suffix}"


def worktree_path(project: Project, feature: NodeId) -> Path:
    """One per feature: the next report replaces it."""
    return repo_dir(project) / ".claude" / "worktrees" / f"{WORKTREE_PREFIX}{handle(feature)}"


def prepare_worktree(project: Project, target: ReportTarget) -> Path:
    """A detached checkout of the feature branch's tip, for the agent to read and for APM to
    commit the report in.
    """
    repo = repo_dir(project)
    path = worktree_path(project, target.feature)
    if path.exists():
        with contextlib.suppress(GitError):
            git(repo, ["worktree", "remove", "--force", "--force", str(path)])
        shutil.rmtree(path, ignore_errors=True)
    with contextlib.suppress(GitError):
        git(repo, ["worktree", "prune"])
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, ["worktree", "add", "--quiet", "--detach", str(path), target.sha])
    return path


def branch_changes(project: Project, target: ReportTarget) -> BranchChanges:
    """What the feature branch changes against its home, for the record."""
    repo = repo_dir(project)
    base = branch_ref(repo, target.home) or default_branch(repo) or target.sha
    stat = git(repo, ["diff", "--stat=120", f"{base}...{target.sha}"])
    commits = git(repo, ["log", "--first-parent", "--format=%h %s", f"{base}..{target.sha}"])
    return BranchChanges(target.branch, target.home, target.sha, stat, commits)


@dataclass(frozen=True, slots=True)
class Prepared:
    target: ReportTarget
    worktree: Path
    prompt: str


def prepare_report(project: Project, store: Store, feature: NodeId, message: str = "") -> Prepared:
    """Everything a report run needs before the agent starts: the branch, a checkout of it, and
    the first message with the record. Fetches, so call it off the event loop.
    """
    target = find_target(project, feature)
    worktree = prepare_worktree(project, target)
    record = gather_record(project, store, feature, branch_changes(project, target))
    prompt = report_prompt(
        load_graph(project),
        feature,
        branch=target.branch,
        sha=target.sha,
        home=target.home,
        path=target.path,
        detail=target.detail,
        record=render_record(record),
        message=message,
    )
    return Prepared(target, worktree, prompt)


# --- committing it ---


def save_report(
    project: Project,
    store: Store,
    target: ReportTarget,
    answer: ReportAnswer,
    *,
    run: str | None,
    worktree: Path | None = None,
) -> FeatureReport:
    """Commits the report to the feature branch and keeps it in the store. If it can't be
    committed or pushed, it is kept anyway, with the reason, and the branch is left as it was.
    """
    text = report_file(answer, target.feature, target.detail)
    sha, error = None, None
    try:
        sha = commit_report(project, target, text, worktree=worktree)
    except (GitError, OSError) as err:
        error = str(err)
    report = FeatureReport(
        node_id=target.feature,
        run_id=run,
        branch=target.branch,
        path=target.path,
        sha=sha,
        error=error,
        markdown=text,
        summary=answer.summary.strip(),
        detail=target.detail,
    )
    store.add_report(report)
    return report


def commit_report(
    project: Project, target: ReportTarget, text: str, *, worktree: Path | None = None
) -> str:
    """Writes `text` to the report's path on the feature branch's latest tip, commits it, and
    pushes the branch: a new commit on top, never a force-push. If someone pushed meanwhile, it
    starts again from the new tip, once. Returns the commit (the tip itself if the report there
    already says exactly this).
    """
    repo = repo_dir(project)
    graph = load_graph(project)
    title = graph[target.feature].title if target.feature in graph else handle(target.feature)
    worktree = worktree if worktree is not None and worktree.is_dir() else None
    if worktree is None:
        worktree = prepare_worktree(project, target)
    for attempt in (1, 2):
        fetch_origin(repo)
        tip = branch_tip(repo, target.branch)
        if tip is None:
            raise GitError(["push"], None, f"the branch {target.branch} is gone")
        git(worktree, ["checkout", "--quiet", "--force", "--detach", tip])
        git(worktree, ["clean", "--quiet", "-fd"])
        path = worktree / target.path
        existed = path.exists()
        if existed and path.read_text(encoding="utf-8") == text:
            return tip
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(worktree, ["add", "--", target.path])
        message = CommitMessage(
            subject=f"{'Update' if existed else 'Add'} the feature report for {title}",
            body=f"Written by APM's feature reporter at {target.detail} detail, from the plan's"
            f" record of feature {handle(target.feature)}.",
            trailers=[("APM-Node", target.feature)],
        )
        sha = commit(worktree, message)
        try:
            _push(repo, worktree, target.branch, sha, tip)
        except GitError:
            if attempt == 2:
                raise
            continue  # the branch moved on: write it again on the new tip
        return sha
    raise AssertionError("unreachable")


def _push(repo: Path, worktree: Path, branch: str, sha: str, tip: str) -> None:
    """A plain push to `origin`, or, with no `origin`, moving the local branch if it is still at
    `tip`.
    """
    try:
        git(repo, ["remote", "get-url", "origin"])
    except GitError:
        git(repo, ["update-ref", f"refs/heads/{branch}", sha, tip])
        return
    git(worktree, ["push", "--quiet", "origin", f"{sha}:refs/heads/{branch}"])
    git(repo, ["fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"])


# --- linking it ---


def report_url(project: Project, branch: str, path: str, pr: str | None = None) -> str | None:
    """The report's page on GitHub: in the repository of the feature's pull request if there is
    one, else of the code repo's remote. None if that isn't on GitHub as far as APM can tell.
    """
    if pr and (ref := parse_pr_url(pr)) is not None:
        return f"https://{ref.host}/{ref.owner}/{ref.repo}/blob/{branch}/{path}"
    config = read_config(project.apm_dir)
    remote = config.code.repos[default_repo(config)].remote
    if remote is None:
        with contextlib.suppress(GitError, ReportRefused):
            remote = git(repo_dir(project), ["remote", "get-url", "origin"]).strip()
    match = re.match(r"(?:https?://|git@|ssh://git@)([^/:]+)", remote or "")
    slug = github_repo(remote)
    if slug is None or match is None or "github" not in match[1]:
        return None
    return f"https://{match[1]}/{slug[0]}/{slug[1]}/blob/{branch}/{path}"


def feature_pr(graph: Graph, feature: NodeId) -> str | None:
    """The feature's pull request: the one recorded on its review step, while it is open."""
    step = graph.review_step(feature)
    if step is None or graph[step].status is Status.DONE:
        return None
    delivery = graph[step].delivery
    return delivery.pr if delivery is not None else None


def link_report(project: Project, pr: str, path: str, url: str | None) -> None:
    """Puts a link to the report at the top of the pull request's description, replacing an
    earlier one. Raises `GhError` if GitHub can't be read or refuses.
    """
    ref = parse_pr_url(pr)
    if ref is None:
        raise GhError(f"{pr} is not a GitHub pull request")
    repo = repo_dir(project)
    try:
        data = json.loads(gh(repo, ["api", *ref.host_args, ref.api_path]))
        body = str(data.get("body") or "")
    except (ValueError, AttributeError) as err:
        raise GhError("`gh api` gave an answer APM couldn't read") from err
    updated = with_link(body, f"{LINK_LABEL} [{path}]({url})" if url else f"{LINK_LABEL} `{path}`")
    if updated != body:
        gh(repo, ["api", *ref.host_args, "-X", "PATCH", ref.api_path, "-f", f"body={updated}"])


def with_link(body: str, line: str) -> str:
    """`body` with `line` as its first paragraph, and no other report link."""
    kept = [text for text in body.splitlines() if not text.startswith(LINK_LABEL)]
    rest = "\n".join(kept).strip()
    return f"{line}\n\n{rest}\n" if rest else f"{line}\n"
