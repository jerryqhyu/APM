"""The in-memory graph (plans.md §2): two adjacency maps over the same node ids, containment
(parent → children) and dependencies (node → what it depends on, and back).

A Graph is read-only. It doesn't check invariants (that's `validate()`), so it stays safe on broken
input: a reference to a node that doesn't exist is not an edge (`validate()` reports it from the
node's own fields), and a containment cycle can't make a walk loop forever.

Everything that is computed rather than stored lives here too, worked out once on first use:
handles, and each node's state (blocked, ready, stacked, progress). It is never stored, so it can't
go stale.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType

from apm.core.ids import handles, resolve_handle
from apm.core.model import Kind, Node, NodeId, Status


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
    """`todo`, and waiting on something it can't stack on: a dependency that isn't delivered, or
    delivered dependencies on separate branches."""
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
    delivery has a branch or a pull request. They stop being listed here once they're `done`.
    """
    stack_top: NodeId | None
    """The one of `stacked_on` to build on, whose branch holds all their work (see
    `Graph.stack_top`). None when there is nothing to stack on, or when `stacked_on` spans separate
    branches; then the node is blocked until all but one of them merge.
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
        kids: dict[NodeId, list[NodeId]] = {}
        dependents: dict[NodeId, list[NodeId]] = {}
        self._deps: dict[NodeId, tuple[NodeId, ...]] = {}
        for node_id in sorted(by_id):  # id order is creation order
            node = by_id[node_id]
            if node.parent in by_id:
                kids.setdefault(node.parent, []).append(node_id)
            self._deps[node_id] = tuple(sorted(d for d in set(node.depends_on) if d in by_id))
            for dep in self._deps[node_id]:
                dependents.setdefault(dep, []).append(node_id)
        review_last = lambda c: (by_id[c].kind is Kind.REVIEW, c)  # noqa: E731
        self._kids = {p: tuple(sorted(c, key=review_last)) for p, c in kids.items()}
        self._dependents = {d: tuple(n) for d, n in dependents.items()}
        self._top = tuple(n for n in sorted(by_id) if by_id[n].parent is None)

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
        """Children of `parent` (None = top-level nodes), in id order, which is creation order,
        except that a review step comes last: it is the feature's finishing step.
        """
        return self._top if parent is None else self._kids.get(parent, ())

    def is_leaf(self, node_id: NodeId) -> bool:
        return not self._kids.get(self[node_id].id)

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
        """All descendants, depth-first in `children` order, not including `node_id` itself."""
        out: list[NodeId] = []
        seen, stack = {self[node_id].id}, list(reversed(self.children(node_id)))
        while stack:
            if (n := stack.pop()) not in seen:
                seen.add(n)
                out.append(n)
                stack.extend(reversed(self._kids.get(n, ())))
        return out

    def review_step(self, parent: NodeId) -> NodeId | None:
        """The review step among `parent`'s children, if it has one."""
        return next((c for c in self.children(parent) if self.nodes[c].kind is Kind.REVIEW), None)

    def is_review(self, node_id: NodeId) -> bool:
        return self[node_id].kind is Kind.REVIEW

    def feature_of(self, node_id: NodeId) -> NodeId | None:
        """The feature whose branch `node_id`'s work goes into: its parent if the parent has a
        review step, else None (the default branch). A review step's own work, the feature
        branch, goes one level up: into the grandparent's feature branch, or the default branch.
        """
        parent = self[node_id].parent
        if parent is None or parent not in self.nodes:
            return None
        if self.is_review(node_id):
            return self.feature_of(parent)
        return parent if self.review_step(parent) is not None else None

    def leaves(self, node_id: NodeId) -> list[NodeId]:
        """The leaves of `node_id`'s subtree: itself if it has no children."""
        return [n for n in (node_id, *self.descendants(node_id)) if not self._kids.get(n)]

    def dependencies(self, node_id: NodeId) -> tuple[NodeId, ...]:
        """The existing nodes `node_id` depends on, in id order."""
        return self._deps[self[node_id].id]

    def dependents(self, node_id: NodeId) -> tuple[NodeId, ...]:
        """The nodes that depend on `node_id`, in id order."""
        return self._dependents.get(self[node_id].id, ())

    def dependency_edges(self) -> Iterable[tuple[NodeId, NodeId]]:
        """Every (node, what it depends on) between existing nodes."""
        return ((n, d) for n, deps in self._deps.items() for d in deps)

    def depends_on_all(self, node_id: NodeId) -> set[NodeId]:
        """What `node_id` depends on, directly or not (not itself, even on a cycle)."""
        return reachable(node_id, self._deps.__getitem__)

    def containment_cycles(self) -> list[list[NodeId]]:
        """Each cycle of parents, in parent → child order. One parent each, so none share a node."""
        cycles, done = [], set()
        for start in sorted(self.nodes):
            path: dict[NodeId, int] = {}
            n: NodeId | None = start
            while n is not None and n in self.nodes and n not in done and n not in path:
                path[n] = len(path)
                n = self.nodes[n].parent
            if n is not None and n in path:
                cycles.append(list(reversed(list(path)[path[n] :])))
            done.update(path)
        return cycles

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
            top = self.stack_top(stacked)
            blocking = (waiting - stacked) if (top or not stacked) else waiting
            todo = node.status is Status.TODO
            under_dropped = any(nodes[a].status is Status.DROPPED for a in ancestors)
            states[node_id] = NodeState(
                blocked=todo and bool(blocking),
                ready=todo and not blocking and not under_dropped,
                waiting_on=tuple(sorted(waiting)),
                stacked_on=tuple(sorted(stacked)),
                stack_top=top,
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
            and not self._kids.get(node_id)
            and delivery is not None
            and bool(delivery.branch or delivery.pr)
        )

    def stack_top(self, delivered: Iterable[NodeId]) -> NodeId | None:
        """The one of `delivered` (delivered but unmerged nodes) to stack new work on: the only
        one, or the one that depends, directly or not, on all the others, so its branch was built
        on theirs and holds their work. None if there are none, or if they're on separate
        branches.
        """
        candidates = sorted(set(delivered))
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        for top in candidates:
            below = self.depends_on_all(top)
            if all(other == top or other in below for other in candidates):
                return top
        return None

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


def reachable[N](start: N, successors: Callable[[N], Iterable[N]]) -> set[N]:
    """Everything reachable from `start`, not counting `start` unless a cycle leads back."""
    seen: set[N] = set()
    stack = list(successors(start))
    while stack:
        if (n := stack.pop()) not in seen:
            seen.add(n)
            stack.extend(successors(n))
    seen.discard(start)
    return seen


def strongly_connected[N](
    nodes: Iterable[N], successors: Callable[[N], Iterable[N]]
) -> list[set[N]]:
    """The strongly connected components (Tarjan's algorithm, without recursion)."""
    index: dict[N, int] = {}
    low: dict[N, int] = {}
    on_stack: set[N] = set()
    stack: list[N] = []
    groups: list[set[N]] = []
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(successors(root)))]
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    group = set()
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        group.add(member)
                        if member == node:
                            break
                    groups.append(group)
            elif child not in index:
                index[child] = low[child] = len(index)
                stack.append(child)
                on_stack.add(child)
                work.append((child, iter(successors(child))))
            elif child in on_stack:
                low[node] = min(low[node], index[child])
    return groups
