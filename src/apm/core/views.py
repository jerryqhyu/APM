"""Reading the plan: everything the CLI, the MCP server and the HTTP API show.

- Text: the compact outline, and one node in full. People read these in the terminal and agents
  read them through the MCP, so they stay short.
- Data: plain dicts for `--json` and the API.
- Search over titles and bodies, and a node's history.

    1c0b9a44 [work/todo·ready] Stream agent output → 7d2e0f11
"""

from dataclasses import asdict, dataclass
from typing import Any

from apm.core.files import read_body
from apm.core.git import log
from apm.core.graph import Graph
from apm.core.lifting import level_view
from apm.core.model import NodeId
from apm.core.project import Project

# --- text ---------------------------------------------------------------------------------------


def state_label(graph: Graph, node_id: NodeId) -> str:
    """The stored status, with the computed part for a todo node: `todo·ready`, `todo·blocked`."""
    status, state = graph[node_id].status, graph.states[node_id]
    if state.blocked:
        return f"{status}·blocked"
    if state.ready:
        return f"{status}·ready"
    return str(status)


def node_line(graph: Graph, node_id: NodeId) -> str:
    node = graph[node_id]
    state = graph.states[node_id]
    names = graph.handles
    line = f"{names[node_id]} [{node.kind}/{state_label(graph, node_id)}] {node.title}"
    if not graph.is_leaf(node_id):
        line += f" ({state.progress.done}/{state.progress.total})"
    if deps := graph.dependencies(node_id):
        line += " → " + ", ".join(names[d] for d in deps)
    if state.dropped_deps:
        line += " ⚠ depends on dropped " + ", ".join(names[d] for d in state.dropped_deps)
    return line


def outline(graph: Graph, root: NodeId | None = None, depth: int | None = None) -> str:
    """The nodes under `root` (None = everything), depth-first, indented by nesting. `depth` limits
    how many levels are shown: 1 = only the children of `root`.
    """
    lines: list[str] = []
    # (node, indent level), walked depth-first without recursion.
    stack = [(child, 0) for child in reversed(graph.children(root))]
    seen: set[NodeId] = set()
    while stack:
        node_id, level = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        lines.append("  " * level + node_line(graph, node_id))
        children = graph.children(node_id)
        if depth is not None and level + 1 >= depth:
            if children:
                lines[-1] += f" [+{len(graph.descendants(node_id))} below]"
            continue
        stack.extend((child, level + 1) for child in reversed(children))
    return "\n".join(lines)


def node_text(project: Project, graph: Graph, node_id: NodeId) -> str:
    """One node in full: its line, id, links, delivery and body."""
    node = graph[node_id]
    lines = [node_line(graph, node_id), f"id: {node_id}"]
    for label, ids in (
        ("parent", [node.parent] if node.parent else []),
        ("child", graph.children(node_id)),
        ("depends on", graph.dependencies(node_id)),
        ("needed by", graph.dependents(node_id)),
        ("waiting on", graph.states[node_id].waiting_on),
    ):
        lines += [f"{label}: {node_line(graph, other)}" for other in ids]
    if (d := node.delivery) is not None:
        parts = [d.repo, d.branch or "", d.pr or "", f"{len(d.commits)} commit(s)"]
        lines.append("delivery: " + " · ".join(p for p in parts if p))
    body = read_body(project.apm_dir, node_id).strip()
    return "\n".join(lines) + ("\n\n" + body if body else "\n\n(no body)")


# --- data ---------------------------------------------------------------------------------------


def node_view(graph: Graph, node_id: NodeId) -> dict[str, Any]:
    node = graph[node_id]
    state = graph.states[node_id]
    return {
        "id": node.id,
        "handle": graph.handles[node.id],
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


def node_ref(graph: Graph, node_id: NodeId) -> dict[str, Any]:
    """Just enough to name a node and link to it."""
    node = graph[node_id]
    return {
        "id": node_id,
        "handle": graph.handles[node_id],
        "title": node.title,
        "status": str(node.status),
    }


def all_nodes_view(graph: Graph) -> list[dict[str, Any]]:
    """Every node, in id order."""
    return [node_view(graph, node_id) for node_id in sorted(graph.nodes)]


def level_json(graph: Graph, root: NodeId | None) -> dict[str, Any]:
    """One level, ready to draw: the root's children with their state, lifted edges, ports."""
    view = level_view(graph, root)
    trail = [] if root is None else [*reversed(graph.ancestors(root)), root]
    return {
        "root": None if root is None else node_view(graph, root),
        "breadcrumb": [node_ref(graph, n) for n in trail],
        "nodes": [node_view(graph, n) for n in view.nodes],
        "edges": [
            {"src": e.src, "dst": e.dst, "count": len(e.underlying), "underlying": e.underlying}
            for e in view.edges
        ],
        "ports": [
            {
                "node": p.node,
                "outside": node_ref(graph, p.outside),
                "direction": p.direction,
                "count": len(p.underlying),
            }
            for p in view.ports
        ],
        "entangled": [list(group) for group in view.entangled],
    }


# --- search and history -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchHit:
    node_id: NodeId
    title: str
    snippet: str
    """The first body line that matches, or "" when only the title does."""


def search(project: Project, graph: Graph, query: str) -> list[SearchHit]:
    """Nodes whose title or body contains every word of `query`, ignoring case. Title matches
    come first, then newest first. It reads the files directly: a plan is small enough that an
    index would only be something else to keep in sync.
    """
    terms = query.lower().split()
    if not terms:
        return []
    in_title: list[SearchHit] = []
    in_body: list[SearchHit] = []
    for node_id in sorted(graph.nodes, reverse=True):
        title = graph[node_id].title
        body = read_body(project.apm_dir, node_id)
        haystack = f"{title}\n{body}".lower()
        if not all(term in haystack for term in terms):
            continue
        snippet = next(
            (
                line.strip()
                for line in body.splitlines()
                if any(term in line.lower() for term in terms) and not line.startswith("#")
            ),
            "",
        )
        hit = SearchHit(node_id, title, snippet[:200])
        (in_title if all(term in title.lower() for term in terms) else in_body).append(hit)
    return in_title + in_body


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    sha: str
    time: int
    """Unix seconds."""
    subject: str
    op: str
    actor: str
    run: str | None


def node_history(project: Project, node_id: NodeId, limit: int = 50) -> list[HistoryEntry]:
    """The plan commits that list the node in `APM-Nodes` (plans.md §3.5), newest first."""
    entries = []
    for entry in log(project.apm_dir):
        if node_id not in entry.trailers.get("APM-Nodes", "").split(","):
            continue
        entries.append(
            HistoryEntry(
                sha=entry.sha,
                time=entry.time,
                subject=entry.subject,
                op=entry.trailers.get("APM-Op", ""),
                actor=entry.trailers.get("APM-Actor", ""),
                run=entry.trailers.get("APM-Run"),
            )
        )
        if len(entries) >= limit:
            break
    return entries
