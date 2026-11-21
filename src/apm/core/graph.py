"""The in-memory graph (plans.md §2), held as two networkx digraphs over the same node ids:

- `tree`: containment. An edge runs parent → child.
- `deps`: dependencies. An edge runs from a node to the node it depends on.

A Graph is read-only. It doesn't check invariants (that's `validate()`), so it stays safe on broken
input: a reference to a node that doesn't exist is not an edge (`validate()` reports it from the
node's own fields), and a containment cycle can't make a walk loop forever.

Everything that is computed rather than stored lives here too, worked out once on first use:
handles, and each node's state (blocked, ready, stacked, progress). It is never stored, so it can't
go stale.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType

import networkx as nx

from apm.core.ids import handles, resolve_handle
from apm.core.model import Node, NodeId, Status


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
    """What follows from a node's status and its surroundings (plans.md §2.3)."""

    blocked: bool
    """`todo`, and something in `waiting_on` that isn't in `stacked_on`."""
    ready: bool
    """`todo`, not blocked, and not under a dropped ancestor. Includes stacked work: check
    `stacked_on` to tell "everything it needs is done" from "it can stack on open work"."""
    waiting_on: tuple[NodeId, ...]
    """Dependencies that aren't `done`, the node's own and its ancestors', in id order. Blocking is
    inherited: a child of a blocked parent is blocked too. A dropped dependency stays here: it never
    unblocks anything.
    """
    stacked_on: tuple[NodeId, ...]
    """The part of `waiting_on` that is delivered but not merged: `in_progress` leaves whose
    delivery has a branch or a pull request. Work can start on top of them (a stacked branch), so
    they don't block; they stop being listed here once they're `done`.
    """
    dropped_deps: tuple[NodeId, ...]
    """The node's own dependencies that are `dropped`. A flag for the user to resolve."""
    progress: Progress
    """Over the leaves of the subtree; for a leaf, itself."""


class Graph:
    def __init__(self, nodes: Iterable[Node]) -> None:
        by_id: dict[NodeId, Node] = {}
        for node in nodes:
            if node.id in by_id:
                raise ValueError(f"Graph: duplicate id {node.id}")
            by_id[node.id] = node
        self.nodes: Mapping[NodeId, Node] = MappingProxyType(by_id)

        tree: nx.DiGraph[NodeId] = nx.DiGraph()
        deps: nx.DiGraph[NodeId] = nx.DiGraph()
        ordered = sorted(by_id)
        tree.add_nodes_from(ordered)
        deps.add_nodes_from(ordered)
        for node_id in ordered:
            node = by_id[node_id]
            if node.parent in by_id:
                tree.add_edge(node.parent, node_id)
            deps.add_edges_from((node_id, dep) for dep in node.depends_on if dep in by_id)
        self.tree: nx.DiGraph[NodeId] = nx.freeze(tree)
        self.deps: nx.DiGraph[NodeId] = nx.freeze(deps)
        self._top = tuple(node_id for node_id in ordered if by_id[node_id].parent is None)

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.nodes

    def __getitem__(self, node_id: NodeId) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise KeyError(f"Graph: unknown node {node_id}") from None

    # --- structure ---

    def children(self, parent: NodeId | None) -> tuple[NodeId, ...]:
        """Children of `parent` (None = top-level nodes), in id order, which is creation order."""
        if parent is None:
            return self._top
        if parent not in self.nodes:
            return ()
        return tuple(sorted(self.tree.successors(parent)))

    def is_leaf(self, node_id: NodeId) -> bool:
        return self.tree.out_degree(self[node_id].id) == 0

    def ancestors(self, node_id: NodeId) -> list[NodeId]:
        """Ancestors, nearest first. Stops at a parent that doesn't exist, or on a cycle."""
        out: list[NodeId] = []
        seen = {node_id}
        parent = self[node_id].parent
        while parent is not None and parent not in seen and (node := self.nodes.get(parent)):
            out.append(parent)
            seen.add(parent)
            parent = node.parent
        return out

    def depth(self, node_id: NodeId) -> int:
        """Number of ancestors: 0 for a top-level node."""
        return len(self.ancestors(node_id))

    def is_ancestor(self, ancestor: NodeId, node_id: NodeId) -> bool:
        """True if `ancestor` is a strict ancestor of `node_id`."""
        return ancestor in self.ancestors(node_id)

    def descendants(self, node_id: NodeId) -> list[NodeId]:
        """All descendants, depth-first in id order, not including `node_id` itself."""
        walk = nx.dfs_preorder_nodes(self.tree, self[node_id].id, sort_neighbors=sorted)
        return [n for n in walk if n != node_id]

    def leaves(self, node_id: NodeId) -> list[NodeId]:
        """The leaves of `node_id`'s subtree: itself if it has no children."""
        return [n for n in (node_id, *self.descendants(node_id)) if self.tree.out_degree(n) == 0]

    def dependencies(self, node_id: NodeId) -> tuple[NodeId, ...]:
        """The existing nodes `node_id` depends on, in id order."""
        return tuple(sorted(self.deps.successors(self[node_id].id)))

    def dependents(self, node_id: NodeId) -> tuple[NodeId, ...]:
        """The nodes that depend on `node_id`, in id order."""
        return tuple(sorted(self.deps.predecessors(self[node_id].id)))

    # --- handles ---

    @cached_property
    def handles(self) -> dict[NodeId, str]:
        """Each node's display handle: its last 8 hex chars, longer only where two collide."""
        return handles(self.nodes)

    def resolve(self, ref: str) -> NodeId:
        """The node a handle or id names. Raises `HandleError` if none or several do."""
        return resolve_handle(ref, self.nodes)

    # --- computed state ---

    @cached_property
    def states(self) -> dict[NodeId, NodeState]:
        nodes = self.nodes
        own_waiting = {
            node_id: {d for d in self.dependencies(node_id) if nodes[d].status is not Status.DONE}
            for node_id in nodes
        }
        stackable = {node_id for node_id in nodes if self._stackable(node_id)}
        states = {}
        for node_id, node in nodes.items():
            ancestors = self.ancestors(node_id)
            waiting = own_waiting[node_id].union(*(own_waiting[a] for a in ancestors))
            stacked = waiting & stackable
            blocking = waiting - stacked
            todo = node.status is Status.TODO
            under_dropped = any(nodes[a].status is Status.DROPPED for a in ancestors)
            states[node_id] = NodeState(
                blocked=todo and bool(blocking),
                ready=todo and not blocking and not under_dropped,
                waiting_on=tuple(sorted(waiting)),
                stacked_on=tuple(sorted(stacked)),
                dropped_deps=tuple(
                    d for d in self.dependencies(node_id) if nodes[d].status is Status.DROPPED
                ),
                progress=self._progress(node_id),
            )
        return states

    def _stackable(self, node_id: NodeId) -> bool:
        """An `in_progress` leaf whose work is pushed (a delivery branch or a pull request), so
        what depends on it can start on top of its branch.
        """
        node = self.nodes[node_id]
        delivery = node.delivery
        return (
            node.status is Status.IN_PROGRESS
            and self.tree.out_degree(node_id) == 0
            and delivery is not None
            and bool(delivery.branch or delivery.pr)
        )

    def ready_leaves(self, root: NodeId | None = None) -> list[NodeId]:
        """What can be worked on now: the ready leaves under `root` (None = the whole project), in
        id order. Stacked ones included (see `NodeState.stacked_on`).
        """
        scope = self.nodes if root is None else self.descendants(root)
        return sorted(n for n in scope if self.is_leaf(n) and self.states[n].ready)

    def _progress(self, node_id: NodeId) -> Progress:
        done = total = 0
        for leaf in self.leaves(node_id):
            lineage = (leaf, *self.ancestors(leaf))
            if any(self[n].status is Status.DROPPED for n in lineage):
                continue
            total += 1
            done += self[leaf].status is Status.DONE
        return Progress(done, total)
