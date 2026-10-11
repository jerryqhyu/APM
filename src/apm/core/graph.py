"""The in-memory graph (plans.md §2.1): nodes by id, a children index, and containment walks.

A Graph is read-only. It doesn't check invariants (that's validate(), P03.2), so its walks must stay
safe on broken input: a missing parent ends a walk, and a containment cycle can't make one loop
forever. Walks are iterative, so deep plans don't hit Python's recursion limit.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from apm.core.model import Node, NodeId


class Graph:
    def __init__(self, nodes: Iterable[Node]) -> None:
        by_id: dict[NodeId, Node] = {}
        for node in nodes:
            if node.id in by_id:
                raise ValueError(f"Graph: duplicate id {node.id}")
            by_id[node.id] = node
        self.nodes: Mapping[NodeId, Node] = MappingProxyType(by_id)
        # Keyed by parent id (None = top level). A missing parent's orphans sit under its id.
        self._children: dict[NodeId | None, list[NodeId]] = {}
        for node_id in sorted(by_id):
            self._children.setdefault(by_id[node_id].parent, []).append(node_id)

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.nodes

    def __getitem__(self, node_id: NodeId) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise KeyError(f"Graph: unknown node {node_id}") from None

    def children(self, parent: NodeId | None) -> tuple[NodeId, ...]:
        """Children of `parent` (None = top-level nodes), in id order, which is creation order."""
        return tuple(self._children.get(parent, ()))

    def is_leaf(self, node_id: NodeId) -> bool:
        return not self._children.get(node_id)

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

    def descendants(self, node_id: NodeId) -> list[NodeId]:
        """All descendants, depth-first in id order, not including `node_id` itself."""
        if node_id not in self.nodes:
            raise KeyError(f"Graph: unknown node {node_id}")
        out: list[NodeId] = []
        seen = {node_id}
        stack = list(reversed(self.children(node_id)))
        while stack:
            child = stack.pop()
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            stack.extend(reversed(self.children(child)))
        return out

    def is_ancestor(self, ancestor: NodeId, node_id: NodeId) -> bool:
        """True if `ancestor` is a strict ancestor of `node_id`."""
        return ancestor in self.ancestors(node_id)
