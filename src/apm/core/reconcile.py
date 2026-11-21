"""Reconciling the plan with the code repo (plans.md §7.4): a node whose work has merged is done.

This is plain code, not an agent: the question "did it merge?" has a definite answer.

- A node with a pull request is merged when `gh` says the PR is.
- Otherwise it is merged when every delivery commit is on the code repo's default branch.
"""

from dataclasses import dataclass
from pathlib import Path

from apm.core.config import read_config
from apm.core.delivery import default_branch, pr_state
from apm.core.git import GitError, is_ancestor
from apm.core.ids import handle
from apm.core.model import Delivery, NodeId, Status
from apm.core.ops import set_status
from apm.core.project import Project, load_graph


@dataclass(frozen=True, slots=True)
class Reconciled:
    node_id: NodeId
    title: str
    reason: str
    """Why it was marked done, e.g. "PR merged"."""


def reconcile(project: Project, *, actor: str = "user") -> list[Reconciled]:
    """Marks every in-progress leaf whose delivery has merged as done. One commit per node."""
    graph = load_graph(project)
    config = read_config(project.apm_dir)
    done = []
    for node_id in sorted(graph.nodes):
        node = graph[node_id]
        if node.status is not Status.IN_PROGRESS or node.delivery is None:
            continue
        repo = config.code.repos.get(node.delivery.repo)
        if repo is None:
            continue
        repo_dir = (project.apm_dir / repo.path).resolve()
        reason = _merged(repo_dir, node.delivery)
        if reason is None:
            continue
        set_status(project, node_id, Status.DONE, note=f"Reconciled: {reason}.", actor=actor)
        done.append(Reconciled(node_id, node.title, reason))
    return done


def _merged(repo_dir: Path, delivery: Delivery) -> str | None:
    if delivery.pr:
        state = pr_state(repo_dir, delivery.pr)
        if state is not None:
            return "PR merged" if state == "MERGED" else None
    if not delivery.commits:
        return None
    target = default_branch(repo_dir)
    if target is None:
        return None
    try:
        merged = all(is_ancestor(repo_dir, commit, target) for commit in delivery.commits)
    except GitError:  # a commit this clone doesn't have
        return None
    return f"its commits are on {target.removeprefix('origin/')}" if merged else None


def describe(items: list[Reconciled]) -> list[str]:
    return [f"{handle(item.node_id)} {item.title}: done ({item.reason})" for item in items]
