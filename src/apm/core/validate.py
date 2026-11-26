"""The graph invariants (plans.md §2.4).

`validate()` checks everything except entanglement (I5), which needs the level views and lives in
`apm.core.lifting`. Errors (I1 to I4, I7) stop a write; warnings (I6) are only reported.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from apm.core.errors import ApmError
from apm.core.graph import Graph, strongly_connected
from apm.core.ids import handle
from apm.core.model import Kind, NodeId, Status

type InvariantCode = Literal["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9"]


@dataclass(frozen=True, slots=True)
class Issue:
    code: InvariantCode
    message: str
    nodes: tuple[NodeId, ...]
    """The nodes involved, the offending node first."""


@dataclass(frozen=True, slots=True)
class Report:
    errors: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()

    @property
    def ok(self) -> bool:
        """True if there are no errors. Warnings don't count."""
        return not self.errors


class InvariantError(ApmError, ValueError):
    """A change was refused because it would break a hard invariant."""

    def __init__(self, issues: Sequence[Issue]) -> None:
        super().__init__("\n".join(f"{i.code}: {i.message}" for i in issues))
        self.issues = tuple(issues)


def validate(graph: Graph) -> Report:
    errors = [
        *_containment(graph),
        *_dependency_cycles(graph),
        *_dependency_targets(graph),
        *_delivery_on_leaves(graph),
        *_review_steps(graph),
    ]
    return Report(tuple(errors), (*_done_parents(graph), *_missing_review_steps(graph)))


def _containment(graph: Graph) -> list[Issue]:
    """I1: every parent exists, and containment has no cycles."""
    issues = [
        Issue("I1", f"{handle(n.id)}: parent {n.parent} doesn't exist", (n.id,))
        for n in graph.nodes.values()
        if n.parent is not None and n.parent not in graph
    ]
    for cycle in sorted(_rotate(c) for c in graph.containment_cycles()):
        issues.append(Issue("I1", f"containment cycle: {_path(cycle)}", tuple(cycle)))
    return issues


def _dependency_cycles(graph: Graph) -> list[Issue]:
    """I2: `depends_on` is acyclic. One issue per tangle, with one cycle from it as the example."""
    issues = []
    deps = graph.dependencies
    tangles = [c for c in strongly_connected(sorted(graph.nodes), deps) if len(c) > 1]
    tangles += [{n} for n in sorted(graph.nodes) if n in deps(n)]
    for tangle in sorted(tangles, key=min):
        cycle = _cycle_through(min(tangle), tangle, deps)
        issues.append(Issue("I2", f"dependency cycle: {_path(cycle)}", tuple(sorted(tangle))))
    return issues


def _dependency_targets(graph: Graph) -> list[Issue]:
    """I4: every dependency exists. I3: none is the node's own ancestor or descendant."""
    issues = []
    for node in graph.nodes.values():
        ancestors = set(graph.ancestors(node.id))
        for dep in node.depends_on:
            if dep not in graph:
                issues.append(
                    Issue("I4", f"{handle(node.id)}: dependency {dep} doesn't exist", (node.id,))
                )
            elif dep in ancestors:
                issues.append(_related(node.id, dep, "ancestor"))
            elif graph.is_ancestor(node.id, dep):
                issues.append(_related(node.id, dep, "descendant"))
    return issues


def _related(node_id: NodeId, dep: NodeId, relation: str) -> Issue:
    message = f"{handle(node_id)} depends on its own {relation} {handle(dep)}"
    return Issue("I3", message, (node_id, dep))


def _review_steps(graph: Graph) -> list[Issue]:
    """I8: a review step is a leaf inside a feature, and a feature has at most one."""
    issues = []
    for node in graph.nodes.values():
        if node.kind is not Kind.REVIEW:
            continue
        if node.parent is None:
            message = f"{handle(node.id)} is a review step, so it must be inside a feature"
            issues.append(Issue("I8", message, (node.id,)))
        if not graph.is_leaf(node.id):
            message = f"{handle(node.id)} is a review step, so it can't have children"
            issues.append(Issue("I8", message, (node.id,)))
    for parent in graph.nodes:
        steps = [c for c in graph.children(parent) if graph[c].kind is Kind.REVIEW]
        if len(steps) > 1:
            names = ", ".join(handle(s) for s in steps)
            message = f"{handle(parent)} has more than one review step ({names})"
            issues.append(Issue("I8", message, (parent, *steps)))
    return issues


def _missing_review_steps(graph: Graph) -> list[Issue]:
    """I9 (a warning): a feature with no review step. Plans from before review steps have these;
    APM adds the step the next time the feature's children change.
    """
    return [
        Issue(
            "I9",
            f"{handle(n.id)} has children but no review step: APM adds one the next time its"
            " children change",
            (n.id,),
        )
        for n in graph.nodes.values()
        if not graph.is_leaf(n.id)
        and graph.review_step(n.id) is None
        and n.status not in (Status.DONE, Status.DROPPED)
    ]


def _delivery_on_leaves(graph: Graph) -> list[Issue]:
    """I7: only leaves carry `delivery`."""
    return [
        Issue("I7", f"{handle(n.id)} has children, so it can't carry a delivery", (n.id,))
        for n in graph.nodes.values()
        if n.delivery is not None and not graph.is_leaf(n.id)
    ]


_UNFINISHED = (Status.TODO, Status.IN_PROGRESS)


def _done_parents(graph: Graph) -> list[Issue]:
    """I6: a `done` parent has no unfinished children."""
    issues = []
    for node in graph.nodes.values():
        if node.status is not Status.DONE:
            continue
        open_children = [c for c in graph.children(node.id) if graph[c].status in _UNFINISHED]
        if open_children:
            count = (
                f"{len(open_children)} unfinished child{'ren' if len(open_children) > 1 else ''}"
            )
            message = f"{handle(node.id)} is done but has {count}"
            issues.append(Issue("I6", message, (node.id, *open_children)))
    return issues


def _cycle_through(
    start: NodeId, within: set[NodeId], deps: Callable[[NodeId], Sequence[NodeId]]
) -> list[NodeId]:
    """A path from `start` back to itself inside a tangle (one always exists), depth first."""
    path, seen = [start], {start}
    branches = [iter([d for d in deps(start) if d in within])]
    while branches:
        step = next(branches[-1], None)
        if step == start:
            return path
        if step is None:
            branches.pop()
            path.pop()
        elif step not in seen:
            seen.add(step)
            path.append(step)
            branches.append(iter([d for d in deps(step) if d in within]))
    return [start]


def _rotate(cycle: list[NodeId]) -> list[NodeId]:
    """The same cycle, starting from its smallest id, so reports are stable."""
    start = cycle.index(min(cycle))
    return cycle[start:] + cycle[:start]


def _path(cycle: Sequence[NodeId]) -> str:
    return " → ".join(handle(n) for n in (*cycle, cycle[0]))
