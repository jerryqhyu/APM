"""Search over titles and bodies. It reads the files directly: a plan is small enough that an
index would only be something else to keep in sync.
"""

from dataclasses import dataclass

from apm.core.body import read_body
from apm.core.graph import Graph
from apm.core.model import NodeId
from apm.core.project import Project


@dataclass(frozen=True, slots=True)
class SearchHit:
    node_id: NodeId
    title: str
    snippet: str
    """The first body line that matches, or "" when only the title does."""


def search(project: Project, graph: Graph, query: str) -> list[SearchHit]:
    """Nodes whose title or body contains every word of `query`, ignoring case. Title matches
    come first, then newest first.
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
