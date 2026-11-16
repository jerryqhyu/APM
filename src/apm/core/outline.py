"""The compact outline (plans.md §6.4): one line per node, indented by depth. It is what people
read in the terminal and what agents read through the MCP, so it stays short.

    1c0b9a44 [work/todo·ready] Stream agent output → 7d2e0f11
"""

from apm.core.graph import Graph
from apm.core.ids import handles
from apm.core.model import NodeId, Status
from apm.core.status import NodeState, compute_states


def state_label(status: Status, state: NodeState) -> str:
    """The stored status, with the computed part for a todo node: `todo·ready`, `todo·blocked`."""
    if state.blocked:
        return f"{status}·blocked"
    if state.ready:
        return f"{status}·ready"
    return str(status)


def format_node_line(
    graph: Graph, node_id: NodeId, states: dict[NodeId, NodeState], names: dict[NodeId, str]
) -> str:
    node = graph[node_id]
    state = states[node_id]
    line = f"{names[node_id]} [{node.kind}/{state_label(node.status, state)}] {node.title}"
    if not graph.is_leaf(node_id):
        line += f" ({state.progress.done}/{state.progress.total})"
    if deps := graph.dependencies(node_id):
        line += " → " + ", ".join(names[d] for d in deps)
    if state.dropped_deps:
        line += " ⚠ depends on dropped " + ", ".join(names[d] for d in state.dropped_deps)
    return line


def format_outline(graph: Graph, root: NodeId | None = None, depth: int | None = None) -> str:
    """The nodes under `root` (None = everything), depth-first. `depth` limits how many levels
    are shown: 1 = only the children of `root`.
    """
    states = compute_states(graph)
    names = handles(graph.nodes)
    lines: list[str] = []
    # (node, indent level), walked depth-first without recursion.
    stack = [(child, 0) for child in reversed(graph.children(root))]
    seen: set[NodeId] = set()
    while stack:
        node_id, level = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        lines.append("  " * level + format_node_line(graph, node_id, states, names))
        children = graph.children(node_id)
        if depth is not None and level + 1 >= depth:
            if children:
                lines[-1] += f" [+{len(graph.descendants(node_id))} below]"
            continue
        stack.extend((child, level + 1) for child in reversed(children))
    return "\n".join(lines)
