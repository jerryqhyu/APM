"""Computed state (plans.md §2.3): blocked, ready and progress. Never stored, so it can't go stale.

Dependencies are inherited: a node also waits on whatever its ancestors wait on, so a child of a
blocked parent is blocked too.
"""

from dataclasses import dataclass

from apm.core.graph import Graph
from apm.core.model import NodeId, Status


@dataclass(frozen=True, slots=True)
class Progress:
    done: int
    """Leaves that are done."""
    total: int
    """Leaves that aren't dropped."""

    @property
    def fraction(self) -> float:
        """0.0 when there is nothing to do."""
        return self.done / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class NodeState:
    blocked: bool
    """`todo`, and something in `waiting_on`."""
    ready: bool
    """`todo`, not blocked, and not under a dropped ancestor."""
    waiting_on: tuple[NodeId, ...]
    """Dependencies that aren't `done`, the node's own and its ancestors', in id order. A dropped
    dependency stays here: it never unblocks anything.
    """
    dropped_deps: tuple[NodeId, ...]
    """The node's own dependencies that are `dropped`. A flag for the user to resolve."""
    progress: Progress
    """Over the leaves of the subtree; for a leaf, itself."""


def compute_states(graph: Graph) -> dict[NodeId, NodeState]:
    nodes = graph.nodes
    own_waiting = {
        node_id: {d for d in graph.dependencies(node_id) if nodes[d].status is not Status.DONE}
        for node_id in nodes
    }
    states = {}
    for node_id, node in nodes.items():
        ancestors = graph.ancestors(node_id)
        waiting = own_waiting[node_id].union(*(own_waiting[a] for a in ancestors))
        todo = node.status is Status.TODO
        under_dropped = any(nodes[a].status is Status.DROPPED for a in ancestors)
        states[node_id] = NodeState(
            blocked=todo and bool(waiting),
            ready=todo and not waiting and not under_dropped,
            waiting_on=tuple(sorted(waiting)),
            dropped_deps=tuple(
                d for d in graph.dependencies(node_id) if nodes[d].status is Status.DROPPED
            ),
            progress=_progress(graph, node_id),
        )
    return states


def ready_leaves(graph: Graph, root: NodeId | None = None) -> list[NodeId]:
    """What can be worked on now: the ready leaves under `root` (None = the whole project), in id
    order.
    """
    states = compute_states(graph)
    scope = graph.nodes if root is None else graph.descendants(root)
    return sorted(n for n in scope if graph.is_leaf(n) and states[n].ready)


def _progress(graph: Graph, node_id: NodeId) -> Progress:
    done = total = 0
    for leaf in graph.leaves(node_id):
        lineage = (leaf, *graph.ancestors(leaf))
        if any(graph[n].status is Status.DROPPED for n in lineage):
            continue
        total += 1
        done += graph[leaf].status is Status.DONE
    return Progress(done, total)
