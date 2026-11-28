"""`apm mcp`: a stdio MCP server over the core. It calls the core directly, so it works whether or
not `apm serve` is running.

What a session may do is set by `APM_RUN_SCOPE`:

- `read`: no writes. For planning agents, which return proposals instead.
- `node:<id>`: status, delivery and body of that one node. For the implementer.
- `project` (the default): everything. For the chat, and for your own interactive sessions.
"""

import functools
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from apm.core import ops
from apm.core.errors import ApmError
from apm.core.files import read_body, render_body
from apm.core.graph import Graph
from apm.core.ids import is_node_id
from apm.core.lifting import check_all
from apm.core.model import Kind, NodeId, Status
from apm.core.mutate import Mutation
from apm.core.project import Project, find_project, load_graph
from apm.core.settings import check_config
from apm.core.store import Store
from apm.core.task_review import GATE_NOTE, ready_gates
from apm.core.views import node_line, node_text, outline, search

SCOPE_ENV = "APM_RUN_SCOPE"
ACTOR_ENV = "APM_ACTOR"
RUN_ENV = "APM_RUN"
NODE_ENV = "APM_NODE"
"""The node a run was started on: what `apm_get_node` shows when it isn't told which."""

INSTRUCTIONS = """\
APM is the project plan: a nested graph of nodes. Each node has a short handle (8 hex chars), a
kind (work or decision), a status (todo, in_progress, done, dropped) and a markdown body. Nodes
nest (one parent each) and can depend on any other node. Refer to nodes by handle.

Start with apm_get_graph for the outline, or apm_ready for what can be worked on now.
"""

NODE_ACTIONS = ("set_status", "attach_delivery", "edit_body")
"""What a `node:<id>` session may do, to its own node only."""


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

    def check(self, action: str, node_id: NodeId | None = None) -> None:
        """Refuses a write this scope doesn't cover."""
        if self.kind == "project":
            return
        if self.kind == "node" and node_id == self.node_id and action in NODE_ACTIONS:
            return
        if self.kind == "read":
            reason = "this session is read-only: return a proposal instead of writing"
        else:
            reason = f"this session may only update status, delivery and body of {self.node_id}"
        raise ToolError(f"not allowed ({SCOPE_ENV}={self}): {reason}")


class Attribution(TypedDict):
    actor: str
    run: str | None


class NewNodeInput(BaseModel):
    title: str
    parent: str | None = Field(None, description="Handle of the parent. Omit for the top level.")
    parent_key: str | None = Field(
        None, description="The parent, when it is another node in this batch: its `key`."
    )
    kind: Kind = Kind.WORK
    intent: str = Field("", description="Why this exists and what outcome it's for.")
    context: str = Field(
        "", description="What a builder must know first: code it builds on, decisions made."
    )
    approach: str = Field(
        "", description="How to build it: files and functions to change, edge cases, tests."
    )
    acceptance: list[str] = Field(default_factory=list, description="Testable criteria.")
    out_of_scope: list[str] = Field(default_factory=list, description="What this does not do.")
    depends_on: list[str] = Field(default_factory=list, description="Handles of existing nodes.")
    tags: list[str] = Field(default_factory=list, description="Labels, e.g. backlog or area/ui.")
    key: str | None = Field(None, description="Temporary name other new nodes can depend on.")
    depends_on_keys: list[str] = Field(default_factory=list, description="Keys in this batch.")


def build_server(project: Project | None = None, env: Mapping[str, str] | None = None) -> MCPServer:
    """`project` defaults to the one found from `APM_PROJECT` or the working directory, looked up
    on each call so the server can start before `apm init` has run.
    """
    env = os.environ if env is None else env
    scope = Scope.parse(env.get(SCOPE_ENV))
    # Who the commits are attributed to (plans.md §3.5).
    by: Attribution = {"actor": env.get(ACTOR_ENV) or "agent:claude", "run": env.get(RUN_ENV)}
    focus = env.get(NODE_ENV) or None
    server = MCPServer("apm", instructions=INSTRUCTIONS)

    def plan() -> tuple[Project, Graph]:
        found = project or find_project(env=env)
        return found, load_graph(found)

    def done(result: Mutation, text: str) -> str:
        return text if result.sha else "nothing to change"

    def tool(fn: Callable[..., str]) -> Callable[..., str]:
        """Registers a tool. Errors the caller can fix come back as the tool's error text."""

        @functools.wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> str:
            try:
                return fn(*args, **kwargs)
            except ApmError as err:
                raise ToolError(str(err)) from None

        return server.tool(structured_output=False)(wrapper)

    # --- read ---

    @tool
    def apm_get_graph(
        root: Annotated[str | None, Field(description="Handle: show only what's under it.")] = None,
        depth: Annotated[int | None, Field(description="Levels to show. Omit for all.")] = None,
    ) -> str:
        """The plan as a compact outline, one node per line, indented by nesting:
        `<handle> [<kind>/<status>·<ready|blocked>] <title> (<done>/<leaves>) → <depends on>`.
        """
        _, graph = plan()
        text = outline(graph, None if root is None else graph.resolve(root), depth)
        return text or "(the plan has no nodes yet)"

    @tool
    def apm_get_node(
        node: Annotated[
            str | None, Field(description="Handle or id. Omit for the node you were started on.")
        ] = None,
    ) -> str:
        """One node in full: state, parent, children, dependencies, delivery and body."""
        found, graph = plan()
        node = node or focus
        if not node:
            raise ToolError("which node? Pass its handle, e.g. apm_get_node(node='1c0b9a44')")
        return node_text(found, graph, graph.resolve(node))

    @tool
    def apm_search(query: str) -> str:
        """Nodes whose title or body contains every word of the query."""
        found, graph = plan()
        hits = search(found, graph, query)
        lines = [
            f"{graph.handles[h.node_id]} {h.title}" + (f" — {h.snippet}" if h.snippet else "")
            for h in hits
        ]
        return "\n".join(lines) or "(no matches)"

    @tool
    def apm_ready(
        root: Annotated[str | None, Field(description="Handle: only what's under it.")] = None,
    ) -> str:
        """What can be worked on now: todo leaves whose dependencies are done. A line marked
        `·stacked` depends on work that is delivered but not merged yet: build it on top of the
        branch named after "stacked on". A line ending "(not ready: …)" needs a passing task
        review first: Implement refuses it until then.
        """
        found, graph = plan()
        ready = graph.ready_leaves(None if root is None else graph.resolve(root))
        gated = ready_gates(
            graph, Store(found.apm_dir), functools.partial(read_body, found.apm_dir)
        )
        lines = [
            node_line(graph, n) + (f"  (not ready: {GATE_NOTE[g]})" if (g := gated.get(n)) else "")
            for n in ready
        ]
        return "\n".join(lines) or "(nothing is ready)"

    @tool
    def apm_check() -> str:
        """Validate the plan and apm.yaml: errors and warnings."""
        project, graph = plan()
        report = check_all(graph)
        config_errors, config_warnings = check_config(project.apm_dir, graph)
        lines = [f"error {i.code}: {i.message}" for i in report.errors]
        lines += [f"error {line}" for line in config_errors]
        lines += [f"warning {i.code}: {i.message}" for i in report.warnings]
        lines += [f"warning {line}" for line in config_warnings]
        return "\n".join(lines) or "ok"

    # --- write ---

    @tool
    def apm_create_nodes(nodes: list[NewNodeInput]) -> str:
        """Create one or more nodes in a single commit. Returns their handles. Give a node a
        `key` and the others in the batch can nest under it (`parent_key`) or depend on it
        (`depends_on_keys`), so a whole subtree is one call.
        """
        scope.check("create")
        found, graph = plan()
        new = [
            ops.NewNode(
                title=n.title,
                parent=None if n.parent is None else graph.resolve(n.parent),
                kind=n.kind,
                depends_on=[graph.resolve(d) for d in n.depends_on],
                key=n.key,
                depends_on_keys=n.depends_on_keys,
                parent_key=n.parent_key,
                body=render_body(
                    intent=n.intent,
                    context=n.context,
                    approach=n.approach,
                    acceptance=n.acceptance,
                    out_of_scope=n.out_of_scope,
                ),
                tags=n.tags,
            )
            for n in nodes
        ]
        result = ops.create_nodes(found, new, **by)
        after = result.graph
        return "\n".join(f"{after.handles[i]} {after[i].title}" for i in result.created)

    @tool
    def apm_update_node(
        node: str,
        title: str | None = None,
        kind: Kind | None = None,
        body: Annotated[str | None, Field(description="The whole new body (markdown).")] = None,
    ) -> str:
        """Change a node's title, kind or body. Only what you pass is changed."""
        found, graph = plan()
        node_id = graph.resolve(node)
        changed = []
        if title is not None or kind is not None:
            scope.check("update", node_id)
            if ops.update_node(found, node_id, title=title, kind=kind, **by).sha:
                changed.append("fields")
        if body is not None:
            scope.check("edit_body", node_id)
            if ops.edit_body(found, node_id, body, **by).sha:
                changed.append("body")
        return f"updated {' and '.join(changed)}" if changed else "nothing to change"

    @tool
    def apm_tag_node(
        node: str,
        add: Annotated[list[str], Field(description="Tags to add.")] = [],  # noqa: B006
        remove: Annotated[list[str], Field(description="Tags to remove.")] = [],  # noqa: B006
    ) -> str:
        """Add or remove a node's tags (labels such as backlog or area/ui). Search finds them
        with `tag:<name>`.
        """
        found, graph = plan()
        node_id = graph.resolve(node)
        scope.check("tag", node_id)
        result = ops.set_tags(found, node_id, add=add, remove=remove, **by)
        tags = " ".join(f"#{t}" for t in result.graph[node_id].tags) or "no tags"
        return f"tags: {tags}" if result.sha else f"nothing to change ({tags})"

    @tool
    def apm_move_node(
        node: str,
        parent: Annotated[str | None, Field(description="New parent. Omit for top level.")] = None,
    ) -> str:
        """Move a node, with everything under it, to another parent."""
        scope.check("move")
        found, graph = plan()
        target = None if not parent else graph.resolve(parent)
        return done(ops.move_node(found, graph.resolve(node), target, **by), "moved")

    @tool
    def apm_link(node: str, depends_on: str) -> str:
        """Record that `node` depends on `depends_on`."""
        scope.check("link")
        found, graph = plan()
        result = ops.link(found, graph.resolve(node), graph.resolve(depends_on), **by)
        return done(result, "linked")

    @tool
    def apm_unlink(node: str, depends_on: str) -> str:
        """Remove the dependency of `node` on `depends_on`."""
        scope.check("unlink")
        found, graph = plan()
        result = ops.unlink(found, graph.resolve(node), graph.resolve(depends_on), **by)
        return done(result, "unlinked")

    @tool
    def apm_set_status(
        node: str,
        status: Status,
        note: Annotated[str, Field(description="Why: recorded in the commit.")] = "",
    ) -> str:
        """Set a node's status: todo, in_progress, done or dropped."""
        found, graph = plan()
        node_id = graph.resolve(node)
        scope.check("set_status", node_id)
        result = ops.set_status(found, node_id, status, note=note, **by)
        return done(result, f"{node}: {status}")

    @tool
    def apm_attach_delivery(
        node: str,
        branch: str | None = None,
        commits: Annotated[list[str] | None, Field(description="Commit SHAs to add.")] = None,
        pr: Annotated[str | None, Field(description="Pull request URL.")] = None,
    ) -> str:
        """Record where a leaf's work landed in the code repo. Leaves only."""
        found, graph = plan()
        node_id = graph.resolve(node)
        scope.check("attach_delivery", node_id)
        result = ops.attach_delivery(
            found, node_id, branch=branch, commits=commits or (), pr=pr, **by
        )
        return done(result, "delivery recorded")

    @tool
    def apm_delete_node(
        node: str,
        recursive: Annotated[bool, Field(description="Also delete everything under it.")] = False,
    ) -> str:
        """Delete a node, and the dependencies on it. A node with children needs `recursive`."""
        scope.check("delete")
        found, graph = plan()
        result = ops.delete_node(found, graph.resolve(node), recursive=recursive, **by)
        return done(result, "deleted")

    @tool
    def apm_merge_nodes(
        target: Annotated[str, Field(description="The node that survives.")],
        others: Annotated[list[str], Field(description="Nodes folded into it, then deleted.")],
        title: Annotated[str | None, Field(description="New title for the merged node.")] = None,
    ) -> str:
        """Merge several nodes into one. The target takes on the others' children, dependencies,
        dependents and bodies.
        """
        scope.check("merge")
        found, graph = plan()
        result = ops.merge_nodes(
            found, graph.resolve(target), [graph.resolve(o) for o in others], title=title, **by
        )
        return done(result, f"merged {len(others) + 1} nodes into {target}")

    return server


def main() -> None:
    build_server().run("stdio")
