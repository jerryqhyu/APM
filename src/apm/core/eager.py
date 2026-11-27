"""Eager mode (plans.md §7.9): a feature built in one run on an *eager branch*, then split into a
chain of task branches, one pull request each, every one showing only its own part; a *redo*
rebuilds the tasks after one whose pull request changed. The plain code around the agents: scope
and gate, branch names, recording a verified split, redo detection and planning, and the UI's view.
Git is `apm.core.split`, the runs `apm.runner.eager`, the state the store's eager tables.
"""

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import networkx as nx

from apm.core.config import ApmConfig
from apm.core.delivery import (
    PullRequest,
    branch_tip,
    ensure_feature_branch,
    fetch_origin,
    read_pr,
)
from apm.core.errors import ApmError
from apm.core.git import GitError, is_ancestor, parents_of, patch_id, ref_tip, tree_of
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import NodeId, Status
from apm.core.ops import attach_delivery, set_status
from apm.core.project import Project, load_graph
from apm.core.project import code_repo as find_code_repo
from apm.core.settings import feature_settings
from apm.core.split import (
    Move,
    Piece,
    diff_text,
    exists,
    publish,
    rebuilt_commits,
    remote_tip,
    source_lines,
)
from apm.core.store import EagerBuild, EagerSlice, Store
from apm.core.task_review import implement_blocker

ACTOR = "apm"
EAGER_PREFIX = "eager/"
BUSY = ("building", "splitting")


class EagerRefused(ApmError):  # noqa: N818 (a refusal, not a failure)
    """An eager build or redo that can't start, for a reason the user can fix."""

    http_status = 409


# --- what a build covers ---


def slug(title: str, limit: int = 32) -> str:
    """The same rule as the implementer's branch names (`apm.runner.agents.slug`)."""
    words = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return words[:limit].rstrip("-") or "node"


def task_branch(graph: Graph, task: NodeId) -> str:
    """A task's branch: the name an implementer would give it."""
    return f"apm/{handle(task)}-{slug(graph[task].title)}"


def eager_branch(graph: Graph, feature: NodeId, round_: int) -> str:
    """`eager/<handle>-<slug>`, and `-2`, `-3`, … for later rounds, so no build ever has to
    overwrite an earlier one's branch.
    """
    name = f"{EAGER_PREFIX}{handle(feature)}-{slug(graph[feature].title)}"
    return name if round_ <= 1 else f"{name}-{round_}"


def eager_feature(config: ApmConfig, graph: Graph, node_id: NodeId) -> NodeId | None:
    """The feature whose eager build covers `node_id`, if eager mode applies: a feature itself,
    or a task's feature. None for a review step, a node outside any feature, or eager off.
    """
    if node_id not in graph or graph.is_review(node_id):
        return None
    if graph.is_leaf(node_id):
        feature = graph.feature_of(node_id)
    else:
        feature = node_id if graph.review_step(node_id) is not None else None
    if feature is None or not feature_settings(config, graph, feature).eager:
        return None
    return feature


def build_tasks(graph: Graph, feature: NodeId) -> list[NodeId]:
    """The To do tasks an eager build of `feature` covers, in dependency order: its own tasks.
    A feature inside it is built on its own, into its own branch.
    """
    lineage = (feature, *graph.ancestors(feature))
    if any(graph[n].status is Status.DROPPED for n in lineage):
        return []
    tasks = [
        child
        for child in graph.children(feature)
        if graph.is_leaf(child) and not graph.is_review(child)
        if graph[child].status is Status.TODO
    ]
    return dependency_order(graph, tasks)


def required_before(graph: Graph, tasks: Sequence[NodeId]) -> dict[NodeId, frozenset[NodeId]]:
    """For each task, the others it depends on, directly or not: they come first in the chain."""
    inside = set(tasks)
    return {t: frozenset(nx.descendants(graph.deps, t) & inside) for t in tasks}


def dependency_order(graph: Graph, tasks: Sequence[NodeId]) -> list[NodeId]:
    """`tasks` with each after everything it depends on; otherwise in the order given."""
    before = required_before(graph, tasks)
    order: list[NodeId] = []
    left = list(tasks)
    while left:
        # A cycle can't happen (the plan refuses one), but never loop on one.
        task = next((t for t in left if before[t] <= set(order)), left[0])
        order.append(task)
        left.remove(task)
    return order


def build_blockers(
    graph: Graph,
    store: Store,
    config: ApmConfig,
    tasks: Sequence[NodeId],
    body: Callable[[NodeId], str],
    locked: Callable[[NodeId], bool],
) -> list[tuple[NodeId, str]]:
    """The gate before an eager build: each task, and why it holds the build back. Every task
    must pass its task review for its current content and not be under review, and everything it
    needs from outside the build must be done: the build starts from the feature branch.
    """
    inside = set(tasks)
    out: list[tuple[NodeId, str]] = []
    for task in tasks:
        if locked(task):
            out.append((task, "under review: wait for its review to finish"))
        elif reason := implement_blocker(graph, store, config, task, body):
            out.append((task, reason))
        elif outside := [n for n in graph.states[task].waiting_on if n not in inside]:
            names = ", ".join(handle(n) for n in outside)
            out.append(
                (
                    task,
                    f"it waits on {names}, which isn't part of this build and isn't done: an"
                    " eager build starts from the feature branch",
                )
            )
    return out


def gate_refusal(graph: Graph, feature: NodeId, blockers: Sequence[tuple[NodeId, str]]) -> str:
    listed = "; ".join(f"{handle(t)} {graph[t].title}: {why}" for t, why in blockers)
    return (
        f'the eager build of {handle(feature)} ("{graph[feature].title}") can\'t start until'
        f" every task in it is ready to build. {listed}"
    )


# --- the build's state ---


EAGER_RUNS = ("apm-eager-builder", "apm-splitter")
"""The agents of an eager pipeline (`apm.runner.agents`)."""


def is_running(store: Store, feature: NodeId) -> bool:
    """True while a build, a redo or a split of `feature` is going: one of its runs is. Its runs
    are recorded as soon as it starts, before its build is.
    """
    running = store.list_runs(node_id=feature, status="running")
    return any(run.agent in EAGER_RUNS for run in running)


def state_of(store: Store, build: EagerBuild | None) -> str | None:
    """The build's state, with a build whose run was cut off (APM stopped) as `failed`."""
    if build is None:
        return None
    if build.state in BUSY and not is_running(store, build.feature_id):
        return "failed"
    return build.state


def split_tasks(store: Store, feature: NodeId) -> set[NodeId]:
    return {s.node_id for s in store.eager_slices(feature)}


def task_refusal(store: Store, config: ApmConfig, graph: Graph, task: NodeId) -> str | None:
    """Why a task of an eager feature can't be implemented on its own, or None."""
    feature = eager_feature(config, graph, task)
    if feature is None or feature == task:
        return None
    if is_running(store, feature):
        return "the eager build of its feature is running: its tasks come from that build"
    if task in split_tasks(store, feature):
        return (
            "this feature is built eagerly: its tasks come from the split; use Redo on the feature"
        )
    return None


def redirect(store: Store, config: ApmConfig, graph: Graph, task: NodeId) -> NodeId | None:
    """The feature to build eagerly instead of implementing a To do `task` alone: while its eager
    feature has no split yet. A task that isn't part of an existing split (one added later), or
    one already in progress, is built on its own: None.
    """
    feature = eager_feature(config, graph, task)
    if feature is None or feature == task or graph[task].status is not Status.TODO:
        return None
    return None if split_tasks(store, feature) else feature


def code_repo(project: Project) -> Path:
    return find_code_repo(project, EagerRefused)


# --- starting a round: the eager branch ---


def start_round(
    project: Project,
    store: Store,
    feature: NodeId,
    tasks: Sequence[NodeId],
    *,
    run: str,
    redo: RedoPlan | None = None,
) -> EagerBuild:
    """Makes the eager branch for a new build (from the feature branch's tip) or a redo (from
    the plan's base), pushes it, and records the round as building.
    """
    repo_dir = code_repo(project)
    fetch_origin(repo_dir)
    base = ensure_feature_branch(project, repo_dir, feature, run=run)
    start = redo.base if redo is not None else branch_tip(repo_dir, base)
    if start is None:
        raise EagerRefused(f"the feature branch {base} isn't there to build from")
    graph = load_graph(project)
    previous = store.eager_build(feature)
    round_ = previous.round + 1 if previous is not None else 1
    while remote_tip(repo_dir, eager_branch(graph, feature, round_)) is not None:
        round_ += 1  # left by a build this store doesn't know about: never reuse it
    name = eager_branch(graph, feature, round_)
    publish(repo_dir, [Move(name, start, None)])
    build = EagerBuild(
        feature_id=feature,
        round=round_,
        branch=name,
        base=base,
        base_sha=start,
        sha=None,
        state="building",
        tasks=tuple(tasks),
        run_id=run,
        pending=redo.as_json() if redo is not None else None,
    )
    return store.save_eager_build(build)


def built_tip(repo_dir: Path, build: EagerBuild) -> str:
    """The eager branch's tip once the builder is done: it must have pushed commits on top of
    where the round started. A branch it only committed to locally is pushed for it.
    """
    fetch_origin(repo_dir)
    tip = remote_tip(repo_dir, build.branch)
    local = ref_tip(repo_dir, f"refs/heads/{build.branch}")
    if local is not None and local != tip and tip is not None and is_ancestor(repo_dir, tip, local):
        publish(repo_dir, [Move(build.branch, local, tip)])
        tip = local
    if tip is None:
        raise EagerRefused(f"the eager branch {build.branch} is gone")
    if tip == build.base_sha:
        raise EagerRefused(f"the build pushed nothing to {build.branch}")
    if not is_ancestor(repo_dir, build.base_sha, tip):
        raise EagerRefused(
            f"{build.branch} no longer starts from {build.base_sha[:12]}: it was rewritten"
        )
    return tip


# --- recording a verified split ---


@dataclass(frozen=True, slots=True)
class PrPlan:
    """A task pull request to open or retarget, in chain order."""

    node_id: NodeId
    base: str
    """The branch it targets: the task before it, or the feature branch for the first."""
    body: str


def record_split(
    project: Project,
    store: Store,
    build: EagerBuild,
    tip: str,
    pieces: Sequence[Piece],
    *,
    run: str,
    warnings: Sequence[str] = (),
) -> list[PrPlan]:
    """Pushes every task's branch (none may exist yet), then records each task's delivery and
    sets it in progress, saves the slices, and marks the build split. Returns the pull requests
    to open, in order. Nothing is recorded if a branch can't be pushed.
    """
    repo_dir = code_repo(project)
    graph = load_graph(project)
    branches = {p.node_id: task_branch(graph, p.node_id) for p in pieces}
    publish(repo_dir, [Move(branches[p.node_id], p.commit, None) for p in pieces])
    slices = [
        _slice(repo_dir, build.feature_id, position, branches[p.node_id], p)
        for position, p in enumerate(pieces)
    ]
    for piece in pieces:
        node = piece.node_id
        attach_delivery(
            project, node, branch=branches[node], commits=[piece.commit], actor=ACTOR, run=run
        )
        if load_graph(project)[node].status is Status.TODO:
            set_status(project, node, Status.IN_PROGRESS, actor=ACTOR, run=run)
    store.save_eager_slices(slices)
    store.save_eager_build(
        replace(
            build,
            sha=tip,
            state="split",
            error=None,
            warnings=tuple(warnings),
            pending=None,
            updated=time.time(),
        )
    )
    return pr_plans(graph, build, slices)


def _slice(repo_dir: Path, feature: NodeId, position: int, branch: str, p: Piece) -> EagerSlice:
    return EagerSlice(
        feature_id=feature,
        node_id=p.node_id,
        position=position,
        branch=branch,
        commit=p.commit,
        parent=p.parent,
        tree=tree_of(repo_dir, p.commit),
        patch_id=patch_id(repo_dir, p.parent, p.commit),
        lines=source_lines(repo_dir, p.parent, p.commit),
    )


def pr_plans(
    graph: Graph,
    build: EagerBuild,
    slices: Sequence[EagerSlice],
    first_base: str | None = None,
) -> list[PrPlan]:
    """The pull requests of a chain: each targets the task before it, the first `first_base`
    (the feature branch unless given), and says so in its description.
    """
    feature = graph[build.feature_id]
    where = (
        f'the eager build of feature {handle(feature.id)} ("{feature.title}"), split by APM from'
        f" `{build.branch}`"
    )
    plans: list[PrPlan] = []
    below = first_base or build.base
    for slice_ in slices:
        part = f"Part {slice_.position + 1} of {where}."
        if below == build.base:
            note = f"{part} The parts after it target it in turn: merge them in order."
        else:
            note = (
                f"{part} It targets `{below}`, the part before it, so it shows only its own"
                f" changes. Merge the parts in order: once that one is merged, APM points this"
                f" pull request at `{build.base}`."
            )
        plans.append(PrPlan(slice_.node_id, below, note))
        below = slice_.branch
    return plans


# --- redo: detecting a task pull request that changed ---


type ChangeKind = Literal["pushed", "merged"]


@dataclass(frozen=True, slots=True)
class Change:
    """A task pull request whose content is no longer what the split gave it."""

    node_id: NodeId
    kind: ChangeKind
    """`pushed`: its branch has new content (review fixes, say). `merged`: it merged into the
    feature branch with content that differs from its slice (edited, or squashed with changes).
    """
    tip: str
    """`pushed`: the branch's tip now. `merged`: the commit that merged it."""


@dataclass(frozen=True, slots=True)
class Findings:
    changes: tuple[Change, ...] = ()
    """In chain order."""
    landed: tuple[tuple[NodeId, str], ...] = ()
    """Tasks that merged into the feature branch exactly as split, with the commit that did."""


def find_changes(
    repo_dir: Path,
    slices: Sequence[EagerSlice],
    prs: Mapping[NodeId, PullRequest | None],
    feature_branch: str,
) -> Findings:
    """Compares what the split recorded with what is there now: each task branch's tip (as last
    fetched) and each pull request's merge. `prs` holds each task's pull request as GitHub has it
    (None if it can't be read).

    - A task merged into the feature branch is unchanged if the merge holds exactly its slice:
      the merge commit's tree is the slice's, or the change it brings has the slice's patch id
      (a squash, a rebase, or a merge on top of other work). Otherwise it changed.
    - A task merged into another branch (the task before it) is left to that branch's check.
    - An open task changed if its branch's tip moved to different content, unless the new tip
      just merged a later task into it (its tree is that task's, and it holds that commit).
    - A deleted branch, or a merge commit this clone doesn't have, tells nothing yet.
    """
    changes: list[Change] = []
    landed: list[tuple[NodeId, str]] = []
    for index, slice_ in enumerate(slices):
        if slice_.landed:
            continue
        pr = prs.get(slice_.node_id)
        if pr is not None and pr.merged:
            merge = pr.merge_commit
            if pr.base != feature_branch or not merge or not exists(repo_dir, merge):
                continue
            if merged_as_split(repo_dir, slice_, merge):
                landed.append((slice_.node_id, merge))
            else:
                changes.append(Change(slice_.node_id, "merged", merge))
            continue
        tip = remote_tip(repo_dir, slice_.branch)
        if tip is None or tip == slice_.commit or tree_of(repo_dir, tip) == slice_.tree:
            continue
        if not _absorbed(repo_dir, tip, slices[index + 1 :]):
            changes.append(Change(slice_.node_id, "pushed", tip))
    return Findings(tuple(changes), tuple(landed))


def merged_as_split(repo_dir: Path, slice_: EagerSlice, merge: str) -> bool:
    if tree_of(repo_dir, merge) == slice_.tree:
        return True
    parents = parents_of(repo_dir, merge)
    return (
        bool(parents)
        and slice_.patch_id is not None
        and (patch_id(repo_dir, parents[0], merge) == slice_.patch_id)
    )


def _absorbed(repo_dir: Path, tip: str, later: Sequence[EagerSlice]) -> bool:
    """True if `tip` is a later task merged into this branch, with nothing else changed."""
    tree = tree_of(repo_dir, tip)
    return any(s.tree == tree and is_ancestor(repo_dir, s.commit, tip) for s in later)


# --- redo: what to rebuild ---


@dataclass(frozen=True, slots=True)
class RedoPlan:
    feature: NodeId
    change: Change
    base: str
    """The commit the rebuilt tasks start from: the changed task's new tip, or the feature
    branch's tip once it merged."""
    base_branch: str
    """What the first rebuilt task's pull request targets: the changed task's branch, or the
    feature branch."""
    tasks: tuple[NodeId, ...]
    """The tasks after the changed one, in chain order: the ones to rebuild."""
    diff: str = ""
    """How the changed task differs from its slice: what the builder is told changed."""

    @property
    def signature(self) -> str:
        """Identifies the change: one automatic redo per change."""
        return f"{self.change.node_id}:{self.change.kind}:{self.change.tip}"

    def as_json(self) -> dict[str, Any]:
        return {
            "node_id": self.change.node_id,
            "kind": self.change.kind,
            "tip": self.change.tip,
            "base": self.base,
            "base_branch": self.base_branch,
            "tasks": list(self.tasks),
            "signature": self.signature,
            "blocked": None,
        }

    @classmethod
    def from_json(cls, feature: NodeId, data: Mapping[str, Any]) -> RedoPlan:
        change = Change(data["node_id"], data["kind"], data["tip"])
        return cls(feature, change, data["base"], data["base_branch"], tuple(data["tasks"]))


type PrReader = Callable[[Path, str], PullRequest | None]


def plan_redo(
    project: Project, store: Store, feature: NodeId, *, read: PrReader = read_pr
) -> RedoPlan | None:
    """Looks for a task pull request of `feature` that changed since the split (fetching first),
    and works out the redo: the earliest change, rebuilt from, and every open task after it.
    Tasks that merged as split are marked landed on the way. None if nothing changed.

    Raises `EagerRefused` when a redo would have to touch what it must not: a task after the
    change that has merged or is done, or whose branch has commits APM didn't make (a second
    change), or that is gone.
    """
    slices = store.eager_slices(feature)
    build = store.eager_build(feature)
    if not slices or build is None:
        raise EagerRefused("nothing to redo: this feature hasn't been split yet")
    repo_dir = code_repo(project)
    fetch_origin(repo_dir)
    graph = load_graph(project)
    prs = {s.node_id: _pr_of(graph, repo_dir, s.node_id, read) for s in slices}
    found = find_changes(repo_dir, slices, prs, build.base)
    if found.landed:
        marked = dict(found.landed)
        slices = [replace(s, landed=marked.get(s.node_id, s.landed)) for s in slices]
        store.save_eager_slices(s for s in slices if s.node_id in marked)
    if not found.changes:
        return None
    change = found.changes[0]
    at = next(i for i, s in enumerate(slices) if s.node_id == change.node_id)
    changed, later = slices[at], slices[at + 1 :]
    if more := found.changes[1:]:
        other = more[0].node_id
        why = "commits APM didn't make" if more[0].kind == "pushed" else "merged with changes"
        raise EagerRefused(
            f"{_named(graph, change.node_id)} changed, but so did {_named(graph, other)}, after it"
            f" ({why}): APM won't rebuild over that. Sort out {_named(graph, other)} by hand"
        )
    for slice_ in later:
        pr = prs.get(slice_.node_id)
        node = graph.nodes.get(slice_.node_id)
        if (
            slice_.landed
            or (pr is not None and pr.merged)
            or node is None
            or node.status
            in (
                Status.DONE,
                Status.DROPPED,
            )
        ):
            raise EagerRefused(
                f"{_named(graph, change.node_id)} changed, but {_named(graph, slice_.node_id)}"
                " after it has already merged or is finished: APM never touches merged work"
            )
        if remote_tip(repo_dir, slice_.branch) is None:
            raise EagerRefused(f"{_named(graph, slice_.node_id)}'s branch {slice_.branch} is gone")
    if change.kind == "pushed":
        base, base_branch = change.tip, changed.branch
    else:
        tip = remote_tip(repo_dir, build.base)
        if tip is None:
            raise EagerRefused(f"the feature branch {build.base} is gone")
        base, base_branch = tip, build.base
    return RedoPlan(
        feature,
        change,
        base,
        base_branch,
        tuple(s.node_id for s in later),
        diff_text(repo_dir, changed.commit, change.tip),
    )


def _pr_of(graph: Graph, repo_dir: Path, node_id: NodeId, read: PrReader) -> PullRequest | None:
    delivery = graph[node_id].delivery if node_id in graph else None
    return read(repo_dir, delivery.pr) if delivery is not None and delivery.pr else None


def _named(graph: Graph, node_id: NodeId) -> str:
    title = graph[node_id].title if node_id in graph else "a deleted task"
    return f'{handle(node_id)} ("{title}")'


def describe_change(graph: Graph, change: Change, feature_branch: str) -> str:
    """The change, in a few words: "new commits on 3f9e2a10 ("Parse")"."""
    if change.kind == "pushed":
        return f"new commits on {_named(graph, change.node_id)} since the split"
    return f"{_named(graph, change.node_id)} merged into {feature_branch} with changes"


def accept_change(store: Store, repo_dir: Path, feature: NodeId, change: Change) -> None:
    """Records a change as the new state of its task, once nothing after it needs rebuilding: a
    pushed tip becomes the task's recorded commit; a merge marks it landed.
    """
    slices = {s.node_id: s for s in store.eager_slices(feature)}
    slice_ = slices.get(change.node_id)
    if slice_ is None:
        return
    if change.kind == "merged":
        store.save_eager_slices([replace(slice_, landed=change.tip)])
        return
    store.save_eager_slices(
        [
            replace(
                slice_,
                commit=change.tip,
                tree=tree_of(repo_dir, change.tip),
                patch_id=patch_id(repo_dir, slice_.parent, change.tip),
            )
        ]
    )


def detect_redos(project: Project, store: Store, *, read: PrReader = read_pr) -> list[RedoPlan]:
    """Redo detection for every split eager feature, after a reconcile: records on each build
    what changed (for the UI, whether or not a redo starts by itself), and returns the redos to
    run. A change with nothing after it to rebuild is simply accepted.

    A build that is running, or whose split failed, is left alone: the failed one's plan is
    what Redo splits again, on the base its round was built from.
    """
    plans: list[RedoPlan] = []
    graph = load_graph(project)
    for build in store.eager_builds():
        feature = build.feature_id
        if feature not in graph or is_running(store, feature) or not store.eager_slices(feature):
            continue
        if state_of(store, build) == "split_failed":
            continue
        pending: dict[str, Any] | None = None
        try:
            plan = plan_redo(project, store, feature, read=read)
        except EagerRefused as err:
            pending = {**(build.pending or {}), "blocked": str(err)}
            plan = None
        except GitError:
            continue  # offline, or a commit this clone doesn't have: look again next time
        if plan is not None and not plan.tasks:
            accept_change(store, code_repo(project), feature, plan.change)
        elif plan is not None:
            pending = plan.as_json()
            plans.append(plan)
        if pending != build.pending:
            store.save_eager_build(replace(build, pending=pending, updated=time.time()))
    return plans


# --- recording a redo ---


def record_redo(
    project: Project,
    store: Store,
    build: EagerBuild,
    tip: str,
    plan: RedoPlan,
    pieces: Sequence[Piece],
    *,
    run: str,
    warnings: Sequence[str] = (),
) -> list[PrPlan]:
    """Moves each rebuilt task's branch forward to its new content (a merge commit on top of what
    APM left there: never a rewrite, and refused if someone pushed to it), records the new
    slices and deliveries, accepts the change that caused the redo, and marks the build split.
    Returns the pull requests to retarget, in order: the first now targets the plan's base branch.
    """
    repo_dir = code_repo(project)
    graph = load_graph(project)
    if plan.base != build.base_sha:
        raise EagerRefused("this rebuild started from another commit than its plan: press Redo")
    old = {s.node_id: s for s in store.eager_slices(build.feature_id)}
    why = describe_change(graph, plan.change, build.base)
    commits = rebuilt_commits(
        repo_dir, pieces, {p.node_id: old[p.node_id].commit for p in pieces}, build.base_sha, why
    )
    publish(
        repo_dir,
        [
            Move(old[p.node_id].branch, new, old[p.node_id].commit)
            for p, new in zip(pieces, commits, strict=True)
        ],
    )
    accept_change(store, repo_dir, build.feature_id, plan.change)
    rebuilt: list[EagerSlice] = []
    below = build.base_sha
    for piece, new in zip(pieces, commits, strict=True):
        before = old[piece.node_id]
        rebuilt.append(
            replace(
                before,
                commit=new,
                parent=below,
                tree=tree_of(repo_dir, new),
                patch_id=patch_id(repo_dir, below, new),
                lines=source_lines(repo_dir, piece.parent, piece.commit),
            )
        )
        attach_delivery(project, piece.node_id, commits=[new], actor=ACTOR, run=run)
        below = new
    store.save_eager_slices(rebuilt)
    store.save_eager_build(
        replace(
            build,
            sha=tip,
            state="split",
            error=None,
            warnings=tuple(warnings),
            pending=None,
            updated=time.time(),
        )
    )
    return pr_plans(graph, build, rebuilt, first_base=plan.base_branch)


# --- what the UI shows ---


def eager_view(
    store: Store, config: ApmConfig, graph: Graph, node_id: NodeId
) -> dict[str, Any] | None:
    """A feature's eager build for its details, or a task's place in one; None if eager mode
    doesn't apply and nothing was built eagerly.
    """
    if node_id not in graph:
        return None
    if graph.is_leaf(node_id):
        return _task_view(store, config, graph, node_id)
    build = store.eager_build(node_id)
    applies = eager_feature(config, graph, node_id) == node_id
    if build is None and not applies:
        return None
    slices = store.eager_slices(node_id)
    pending = build.pending if build is not None else None
    stale = set(pending.get("tasks", [])) if pending and not pending.get("blocked") else set()
    return {
        "applies": applies,
        "auto_redo": config.automation.eager_redo,
        "build": None if build is None else _build_view(store, build),
        "tasks": [_slice_view(graph, s, stale) for s in slices if s.node_id in graph],
        "pending": _pending_view(graph, pending),
        "redo": redo_hint(store, build, slices),
    }


def _build_view(store: Store, build: EagerBuild) -> dict[str, Any]:
    return {
        "round": build.round,
        "branch": build.branch,
        "base": build.base,
        "base_sha": build.base_sha,
        "sha": build.sha,
        "state": state_of(store, build),
        "run_id": build.run_id,
        "error": build.error,
        "warnings": list(build.warnings),
        "tasks": list(build.tasks),
        "updated": build.updated,
    }


def _slice_view(graph: Graph, slice_: EagerSlice, stale: set[NodeId]) -> dict[str, Any]:
    node = graph[slice_.node_id]
    delivery = node.delivery
    if slice_.landed or node.status is Status.DONE:
        state = "merged"
    elif slice_.node_id in stale:
        state = "stale"
    else:
        state = "open"
    return {
        "id": node.id,
        "handle": graph.handles[node.id],
        "title": node.title,
        "status": str(node.status),
        "position": slice_.position,
        "branch": slice_.branch,
        "commit": slice_.commit,
        "lines": slice_.lines,
        "pr": delivery.pr if delivery is not None else None,
        "state": state,
    }


def _pending_view(graph: Graph, pending: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not pending or "node_id" not in pending:
        return {"blocked": pending["blocked"]} if pending and pending.get("blocked") else None
    node_id = pending["node_id"]
    title = graph[node_id].title if node_id in graph else "a deleted task"
    return {
        "node": {"id": node_id, "handle": handle(node_id), "title": title},
        "kind": pending["kind"],
        "blocked": pending.get("blocked"),
    }


def redo_hint(
    store: Store, build: EagerBuild | None, slices: Sequence[EagerSlice]
) -> dict[str, Any]:
    """What Redo would do now, or why it can't: `{"action": ..., "why": ...}`."""
    state = state_of(store, build)
    if build is None:
        return {"action": None, "why": "nothing has been built eagerly yet: press Implement"}
    if is_running(store, build.feature_id):
        return {"action": None, "why": "the eager build is running"}
    if state == "split_failed":
        return {"action": "split", "why": "split the build again"}
    if not slices:
        return {"action": None, "why": "the build failed before it was split: press Implement"}
    pending = build.pending or {}
    if pending.get("blocked"):
        return {"action": "rebuild", "why": pending["blocked"]}
    if pending.get("node_id") or state == "failed":
        return {"action": "rebuild", "why": "rebuild the tasks after the one that changed"}
    return {"action": "rebuild", "why": "look for a task that changed, and rebuild what follows it"}


def _task_view(
    store: Store, config: ApmConfig, graph: Graph, task: NodeId
) -> dict[str, Any] | None:
    feature = eager_feature(config, graph, task)
    slice_ = store.eager_slice_of(task)
    if feature is None and slice_ is None:
        return None
    owner = feature or (slice_.feature_id if slice_ is not None else None)
    return {
        "feature": None
        if owner is None or owner not in graph
        else {
            "id": owner,
            "handle": graph.handles[owner],
            "title": graph[owner].title,
        },
        "in_split": slice_ is not None,
        "blocker": task_refusal(store, config, graph, task),
        "redirect": redirect(store, config, graph, task) is not None,
    }
