"""The in-memory graph (plans.md §2.1), held as two networkx digraphs over the same node ids:

- `tree`: containment. An edge runs parent → child.
- `deps`: dependencies. An edge runs from a node to the node it depends on.

A Graph is read-only. It doesn't check invariants (that's validate()), so it stays safe on broken
input: a reference to a node that doesn't exist is not an edge (validate() reports it from the
node's own fields), and a containment cycle can't make a walk loop forever.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

import networkx as nx

from apm.core.model import Node, NodeId


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
