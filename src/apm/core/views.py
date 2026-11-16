"""Plain-data views of the plan, shared by `--json` output, the HTTP API and the MCP server."""

from dataclasses import asdict
from typing import Any

from apm.core.graph import Graph
from apm.core.ids import handles
from apm.core.lifting import LevelView, level_view
from apm.core.model import NodeId
from apm.core.status import NodeState, compute_states


def node_view(
    graph: Graph, states: dict[NodeId, NodeState], names: dict[NodeId, str], node_id: NodeId
) -> dict[str, Any]:
    node = graph[node_id]
    state = states[node_id]
    return {
        "id": node.id,
        "handle": names[node.id],
        "parent": node.parent,
        "title": node.title,
        "kind": str(node.kind),
        "status": str(node.status),
        "blocked": state.blocked,
        "ready": state.ready,
        "waiting_on": list(state.waiting_on),
        "dropped_deps": list(state.dropped_deps),
        "depends_on": list(graph.dependencies(node_id)),
        "dependents": list(graph.dependents(node_id)),
        "children": len(graph.children(node_id)),
        "progress": {"done": state.progress.done, "total": state.progress.total},
        "delivery": None if node.delivery is None else asdict(node.delivery),
    }


def all_nodes_view(graph: Graph) -> list[dict[str, Any]]:
    """Every node, in id order."""
    states = compute_states(graph)
    names = handles(graph.nodes)
    return [node_view(graph, states, names, node_id) for node_id in sorted(graph.nodes)]


def level_json(graph: Graph, root: NodeId | None) -> dict[str, Any]:
    """One level, ready to draw: the root's children with their state, lifted edges, ports."""
    view: LevelView = level_view(graph, root)
    states = compute_states(graph)
    names = handles(graph.nodes)
    trail = [] if root is None else [*reversed(graph.ancestors(root)), root]
    return {
        "root": None if root is None else node_view(graph, states, names, root),
        "breadcrumb": [{"id": n, "handle": names[n], "title": graph[n].title} for n in trail],
        "nodes": [node_view(graph, states, names, n) for n in view.nodes],
        "edges": [
            {"src": e.src, "dst": e.dst, "count": len(e.underlying), "underlying": e.underlying}
            for e in view.edges
        ],
        "ports": [
            {
                "node": p.node,
                "outside": {"id": p.outside, "handle": names[p.outside]}
                | {"title": graph[p.outside].title},
                "direction": p.direction,
                "count": len(p.underlying),
            }
            for p in view.ports
        ],
        "entangled": [list(group) for group in view.entangled],
    }
