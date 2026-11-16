"""`apm mcp`: a stdio MCP server over the core. It calls the core directly, so it works whether or
not `apm serve` is running.

What a session may do is set by `APM_RUN_SCOPE`:

- `read`: no writes. For planning agents, which return proposals instead.
- `node:<id>`: status, delivery and body of that one node. For the implementer.
- `project` (the default): everything. For your own interactive sessions.
"""

import functools
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from apm.core import ops
from apm.core.body import read_body, render_body
from apm.core.config import ConfigError
from apm.core.git import GitError
from apm.core.graph import Graph
from apm.core.ids import HandleError, handles, is_node_id, resolve_handle
from apm.core.lifting import check_all
from apm.core.lock import LockTimeoutError
from apm.core.model import Kind, NodeId, Status
from apm.core.mutate import DirtyPlanError, Mutation, OpError
from apm.core.ndjson import GraphParseError, InvalidNodeError
from apm.core.outline import format_node_line, format_outline
from apm.core.project import Project, ProjectError, find_project, load_graph
from apm.core.search import search
from apm.core.status import compute_states, ready_leaves
from apm.core.validate import InvariantError

SCOPE_ENV = "APM_RUN_SCOPE"
ACTOR_ENV = "APM_ACTOR"
RUN_ENV = "APM_RUN"

INSTRUCTIONS = """\
APM is the project plan: a nested graph of nodes. Each node has a short handle (8 hex chars), a
kind (work or decision), a status (todo, in_progress, done, dropped) and a markdown body. Nodes
nest (one parent each) and can depend on any other node. Refer to nodes by handle.

Start with apm_get_graph for the outline, or apm_ready for what can be worked on now.
"""

EXPECTED_ERRORS = (
    ProjectError,
    HandleError,
    OpError,
    InvariantError,
    GraphParseError,
    InvalidNodeError,
    ConfigError,
    DirtyPlanError,
    GitError,
    LockTimeoutError,
)


@dataclass(frozen=True, slots=True)
class Scope:
    kind: Literal["read", "node", "project"]
    node_id: NodeId | None = None

    @classmethod
    def parse(cls, value: str | None) -> Scope:
        if not value or value == "project":
            return cls("project")
        if value == "read":
            return cls("read")
        if value.startswith("node:") and is_node_id(node_id := value.removeprefix("node:")):
            return cls("node", node_id)
        raise ValueError(f'{SCOPE_ENV} must be "read", "project" or "node:<id>", not {value!r}')

    def __str__(self) -> str:
        return f"node:{self.node_id}" if self.kind == "node" else self.kind


class NewNodeInput(BaseModel):
    title: str
    parent: str | None = Field(None, description="Handle of the parent. Omit for the top level.")
    kind: Kind = Kind.WORK
    intent: str = Field("", description="Why this exists and what outcome it's for.")
    acceptance: list[str] = Field(default_factory=list, description="Testable criteria.")
    depends_on: list[str] = Field(default_factory=list, description="Handles of existing nodes.")
    key: str | None = Field(None, description="Temporary name other new nodes can depend on.")
    depends_on_keys: list[str] = Field(default_factory=list, description="Keys in this batch.")


def tool_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Sends the errors a caller can cause back as the tool's error text."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except EXPECTED_ERRORS as err:
            raise ToolError(str(err)) from None

    return wrapper


def build_server(project: Project | None = None, env: Mapping[str, str] | None = None) -> MCPServer:
    """`project` defaults to the one found from `APM_PROJECT` or the working directory, looked up
    on each call so the server can start before `apm init` has run.
    """
    env = os.environ if env is None else env
    scope = Scope.parse(env.get(SCOPE_ENV))
    actor = env.get(ACTOR_ENV) or "agent:claude"
    run = env.get(RUN_ENV) or None
    server = MCPServer("apm", instructions=INSTRUCTIONS)

    def current() -> tuple[Project, Graph]:
        found = project or find_project(env=env)
        return found, load_graph(found)

    def allow(action: str, node_id: NodeId | None = None) -> None:
        """Refuses a write this session's scope doesn't cover."""
        if scope.kind == "project":
            return
        if (
            scope.kind == "node"
            and node_id == scope.node_id
            and action in ("set_status", "attach_delivery", "edit_body")
        ):
            return
        if scope.kind == "read":
            reason = "this session is read-only: return a proposal instead of writing"
        else:
            reason = f"this session may only update status, delivery and body of {scope.node_id}"
        raise ToolError(f"not allowed ({SCOPE_ENV}={scope}): {reason}")

    def done(result: Mutation, text: str) -> str:
        return text if result.sha else "nothing to change"

    def tool(fn: Callable[..., str]) -> Callable[..., str]:
        return server.tool(structured_output=False)(tool_errors(fn))

    # --- read ---

    @tool
    def apm_get_graph(
        root: Annotated[str | None, Field(description="Handle: show only what's under it.")] = None,
        depth: Annotated[int | None, Field(description="Levels to show. Omit for all.")] = None,
    ) -> str:
        """The plan as a compact outline, one node per line, indented by nesting:
        `<handle> [<kind>/<status>·<ready|blocked>] <title> (<done>/<leaves>) → <depends on>`.
        """
        _, graph = current()
        scope_id = None if root is None else resolve_handle(root, graph.nodes)
        return format_outline(graph, scope_id, depth) or "(the plan has no nodes yet)"

    @tool
    def apm_get_node(node: Annotated[str, Field(description="Handle or id.")]) -> str:
        """One node in full: state, parent, children, dependencies, delivery and body."""
        found, graph = current()
        node_id = resolve_handle(node, graph.nodes)
        states, names = compute_states(graph), handles(graph.nodes)
        n = graph[node_id]
        lines = [format_node_line(graph, node_id, states, names), f"id: {node_id}"]
        for label, ids in (
            ("parent", [n.parent] if n.parent else []),
            ("child", graph.children(node_id)),
            ("depends on", graph.dependencies(node_id)),
            ("needed by", graph.dependents(node_id)),
            ("waiting on", states[node_id].waiting_on),
        ):
            lines += [f"{label}: {format_node_line(graph, o, states, names)}" for o in ids]
        if (d := n.delivery) is not None:
            lines.append(f"delivery: repo={d.repo} branch={d.branch} pr={d.pr} commits={d.commits}")
        body = read_body(found.apm_dir, node_id).strip()
        return "\n".join(lines) + ("\n\n" + body if body else "\n\n(no body)")

    @tool
    def apm_search(query: str) -> str:
        """Nodes whose title or body contains every word of the query."""
        found, graph = current()
        names = handles(graph.nodes)
        hits = search(found, graph, query)
        return (
            "\n".join(
                f"{names[h.node_id]} {h.title}" + (f" — {h.snippet}" if h.snippet else "")
                for h in hits
            )
            or "(no matches)"
        )

    @tool
    def apm_ready(
        root: Annotated[str | None, Field(description="Handle: only what's under it.")] = None,
    ) -> str:
        """What can be worked on now: leaves that are todo with every dependency done."""
        _, graph = current()
        scope_id = None if root is None else resolve_handle(root, graph.nodes)
        states, names = compute_states(graph), handles(graph.nodes)
        lines = [format_node_line(graph, n, states, names) for n in ready_leaves(graph, scope_id)]
        return "\n".join(lines) or "(nothing is ready)"

    @tool
    def apm_check() -> str:
        """Validate the plan: invariant errors and warnings."""
        _, graph = current()
        report = check_all(graph)
        lines = [f"error {i.code}: {i.message}" for i in report.errors]
        lines += [f"warning {i.code}: {i.message}" for i in report.warnings]
        return "\n".join(lines) or "ok"

    # --- write ---

    @tool
    def apm_create_nodes(nodes: list[NewNodeInput]) -> str:
        """Create one or more nodes in a single commit. Returns their handles."""
        allow("create")
        found, graph = current()
        new = [
            ops.NewNode(
                title=n.title,
                parent=None if n.parent is None else resolve_handle(n.parent, graph.nodes),
                kind=n.kind,
                depends_on=[resolve_handle(d, graph.nodes) for d in n.depends_on],
                key=n.key,
                depends_on_keys=n.depends_on_keys,
                body=render_body(intent=n.intent, acceptance=n.acceptance),
            )
            for n in nodes
        ]
        result = ops.create_nodes(found, new, actor=actor, run=run)
        names = handles(result.graph.nodes)
        return "\n".join(f"{names[i]} {result.graph[i].title}" for i in result.created)

    @tool
    def apm_update_node(
        node: str,
        title: str | None = None,
        kind: Kind | None = None,
        body: Annotated[str | None, Field(description="The whole new body (markdown).")] = None,
    ) -> str:
        """Change a node's title, kind or body. Only what you pass is changed."""
        found, graph = current()
        node_id = resolve_handle(node, graph.nodes)
        changed = []
        if title is not None or kind is not None:
            allow("update", node_id)
            if ops.update_node(found, node_id, title=title, kind=kind, actor=actor, run=run).sha:
                changed.append("fields")
        if body is not None:
            allow("edit_body", node_id)
            if ops.edit_body(found, node_id, body, actor=actor, run=run).sha:
                changed.append("body")
        return f"updated {' and '.join(changed)}" if changed else "nothing to change"

    @tool
    def apm_move_node(
        node: str,
        parent: Annotated[
            str | None, Field(description="New parent. Omit for the top level.")
        ] = None,
    ) -> str:
        """Move a node, with everything under it, to another parent."""
        allow("move")
        found, graph = current()
        target = None if not parent else resolve_handle(parent, graph.nodes)
        moved = ops.move_node(
            found, resolve_handle(node, graph.nodes), target, actor=actor, run=run
        )
        return done(moved, "moved")

    @tool
    def apm_link(node: str, depends_on: str) -> str:
        """Record that `node` depends on `depends_on`."""
        allow("link")
        found, graph = current()
        src, dst = (resolve_handle(h, graph.nodes) for h in (node, depends_on))
        return done(ops.link(found, src, dst, actor=actor, run=run), "linked")

    @tool
    def apm_unlink(node: str, depends_on: str) -> str:
        """Remove the dependency of `node` on `depends_on`."""
        allow("unlink")
        found, graph = current()
        src, dst = (resolve_handle(h, graph.nodes) for h in (node, depends_on))
        return done(ops.unlink(found, src, dst, actor=actor, run=run), "unlinked")

    @tool
    def apm_set_status(
        node: str,
        status: Status,
        note: Annotated[str, Field(description="Why: recorded in the commit.")] = "",
    ) -> str:
        """Set a node's status: todo, in_progress, done or dropped."""
        found, graph = current()
        node_id = resolve_handle(node, graph.nodes)
        allow("set_status", node_id)
        result = ops.set_status(found, node_id, status, note=note, actor=actor, run=run)
        return done(result, f"{node}: {status}")

    @tool
    def apm_attach_delivery(
        node: str,
        branch: str | None = None,
        commits: Annotated[list[str] | None, Field(description="Commit SHAs to add.")] = None,
        pr: Annotated[str | None, Field(description="Pull request URL.")] = None,
    ) -> str:
        """Record where a leaf's work landed in the code repo. Leaves only."""
        found, graph = current()
        node_id = resolve_handle(node, graph.nodes)
        allow("attach_delivery", node_id)
        result = ops.attach_delivery(
            found, node_id, branch=branch, commits=commits or (), pr=pr, actor=actor, run=run
        )
        return done(result, "delivery recorded")

    return server


def main() -> None:
    build_server().run("stdio")
