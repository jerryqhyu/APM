"""Lifting (plans.md §2.2): showing one level of the graph.

A dependency is stored once, between the two nodes where it's true, at any depth. To show the
children of a node R, each edge is mapped onto the children of R that contain its two ends.
"""

from dataclasses import dataclass
from typing import Literal

from apm.core.graph import Graph, strongly_connected
from apm.core.ids import handle
from apm.core.model import NodeId
from apm.core.validate import Issue, Report, validate

type Edge = tuple[NodeId, NodeId]
"""(node, the node it depends on)."""


@dataclass(frozen=True, slots=True)
class LiftedEdge:
    """`src` depends on `dst`: both are children of the level's root."""

    src: NodeId
    dst: NodeId
    underlying: tuple[Edge, ...]
    """The stored edges this one stands for, sorted. Its length is the count badge."""


@dataclass(frozen=True, slots=True)
class Port:
    """An edge that leaves the level: one end is inside `node`, the other outside the root."""

    node: NodeId
    """The child of the root that holds the inside end."""
    outside: NodeId
    """The node at the outside end."""
    direction: Literal["out", "in"]
    """`out`: `node` depends on `outside`. `in`: `outside` depends on `node`."""
    underlying: tuple[Edge, ...]


@dataclass(frozen=True, slots=True)
class LevelView:
    root: NodeId | None
    """None = the top level."""
    nodes: tuple[NodeId, ...]
    """The root's children, in id order."""
    edges: tuple[LiftedEdge, ...]
    ports: tuple[Port, ...]
    internal: tuple[Edge, ...]
    """Stored edges with both ends inside the same child: hidden at this level."""
    entangled: tuple[tuple[NodeId, ...], ...]
    """I5: groups of children whose lifted edges form a cycle."""


def level_view(graph: Graph, root: NodeId | None = None) -> LevelView:
    children = graph.children(root)
    # owner[n] = the child of the root that contains n (or is n).
    owner: dict[NodeId, NodeId] = {}
    for child in children:
        owner[child] = child
        owner.update(dict.fromkeys(graph.descendants(child), child))

    lifted: dict[tuple[NodeId, NodeId], list[Edge]] = {}
    ports: dict[tuple[NodeId, NodeId, Literal["out", "in"]], list[Edge]] = {}
    internal: list[Edge] = []
    for u, v in graph.dependency_edges():
        a, b = owner.get(u), owner.get(v)
        if a is not None and b is not None:
            if a == b:
                internal.append((u, v))
                continue
            lifted.setdefault((a, b), []).append((u, v))
        elif a is not None:
            ports.setdefault((a, v, "out"), []).append((u, v))
        elif b is not None:
            ports.setdefault((b, u, "in"), []).append((u, v))

    out: dict[NodeId, list[NodeId]] = {child: [] for child in children}
    for a, b in lifted:
        out[a].append(b)
    return LevelView(
        root=root,
        nodes=children,
        edges=tuple(LiftedEdge(a, b, tuple(sorted(lifted[a, b]))) for a, b in sorted(lifted)),
        ports=tuple(
            Port(node, outside, direction, tuple(sorted(edges)))
            for (node, outside, direction), edges in sorted(ports.items())
        ),
        internal=tuple(sorted(internal)),
        entangled=tuple(
            sorted(
                tuple(sorted(group))
                for group in strongly_connected(children, out.__getitem__)
                if len(group) > 1
            )
        ),
    )


def entanglements(graph: Graph) -> list[Issue]:
    """I5 at every level: the top level, and under every node that has children."""
    roots = [None, *(n for n in sorted(graph.nodes) if not graph.is_leaf(n))]
    issues = []
    for root in roots:
        for group in level_view(graph, root).entangled:
            where = "at the top level" if root is None else f"under {handle(root)}"
            names = ", ".join(handle(n) for n in group)
            issues.append(Issue("I5", f"entanglement {where}: {names}", group))
    return issues


def check_all(graph: Graph) -> Report:
    """Every invariant: `validate()` plus entanglement. Entanglement is only looked for in a graph
    with no errors, since level views of a broken tree mean little.
    """
    report = validate(graph)
    if not report.ok:
        return report
    return Report(report.errors, (*report.warnings, *entanglements(graph)))
