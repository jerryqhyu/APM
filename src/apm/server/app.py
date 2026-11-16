"""The HTTP and WebSocket API (plans.md §6.3). The web UI is its only intended client."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from apm import __version__
from apm.core import ops
from apm.core.body import read_body, render_body
from apm.core.config import ConfigError, read_config
from apm.core.git import GitError, rev_parse
from apm.core.graph import Graph
from apm.core.history import node_history
from apm.core.ids import HandleError, handles, resolve_handle
from apm.core.lifting import check_all
from apm.core.lock import LockTimeoutError
from apm.core.model import Kind, NodeId, Status
from apm.core.mutate import DirtyPlanError, OpError
from apm.core.ndjson import GraphParseError, InvalidNodeError
from apm.core.project import Project, load_graph
from apm.core.proposals import (
    ProposalError,
    ProposalResolvedError,
    accept_proposal,
    reject_proposal,
)
from apm.core.search import search
from apm.core.status import compute_states, ready_leaves
from apm.core.store import Store
from apm.core.validate import InvariantError
from apm.core.views import all_nodes_view, level_json, node_view
from apm.runner.agents import AGENTS, RunRefused
from apm.runner.runner import Runner, RunnerBusy
from apm.server.hub import Hub
from apm.server.security import bearer, host_is_loopback, origin_allowed, token_matches

STATIC_DIR = Path(__file__).parent / "static"
WATCH_INTERVAL_S = 0.5

UI_MISSING = """<!doctype html><meta charset="utf-8"><title>APM</title>
<body style="font-family: system-ui; max-width: 40rem; margin: 4rem auto; line-height: 1.5">
<h1>The web UI isn't built yet</h1>
<p>The server is running, but this install has no UI files. From the APM source folder, run:</p>
<pre>cd ui &amp;&amp; pnpm install &amp;&amp; pnpm build</pre>
<p>then restart <code>apm serve</code>.</p></body>"""


class CreateNode(BaseModel):
    title: str
    parent: str | None = None
    kind: Kind = Kind.WORK
    depends_on: list[str] = Field(default_factory=list)
    intent: str = ""


class PatchNode(BaseModel):
    title: str | None = None
    kind: Kind | None = None
    status: Status | None = None
    body: str | None = None
    note: str = ""


class MoveNode(BaseModel):
    parent: str | None = None


class Edge(BaseModel):
    src: str
    dst: str


class StartRun(BaseModel):
    agent: str
    node_id: str
    guidance: str = ""
    critic: bool = True


class ContinueRun(BaseModel):
    message: str


class AcceptProposal(BaseModel):
    children: list[dict[str, Any]] | None = None
    """The children to create, if you edited or trimmed the proposal. Default: all of them."""


# Errors a client can cause → the HTTP status that says so.
ERROR_STATUS: list[tuple[type[Exception], int]] = [
    (ProposalResolvedError, 409),
    (RunnerBusy, 409),
    (RunRefused, 422),
    (HandleError, 404),
    (KeyError, 404),
    (ProposalError, 422),
    (InvariantError, 422),
    (OpError, 422),
    (DirtyPlanError, 409),
    (InvalidNodeError, 422),
    (GraphParseError, 422),
    (ConfigError, 422),
    (LockTimeoutError, 503),
    (GitError, 500),
]


def create_app(
    project: Project,
    token: str,
    *,
    static_dir: Path | None = STATIC_DIR,
    extra_origins: frozenset[str] = frozenset(),
) -> FastAPI:
    store = Store(project.apm_dir)
    hub = Hub()
    runner = Runner(project, store, hub.publish)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        hub.bind(asyncio.get_running_loop())
        watcher = asyncio.create_task(_watch_head(project, hub))
        try:
            yield
        finally:
            watcher.cancel()
            await runner.shutdown()

    app = FastAPI(
        title="APM", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.runner = runner
    app.state.store = store
    app.state.hub = hub

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host")
        if not host_is_loopback(host):
            return _error(403, "this server only answers on a loopback address")
        if not origin_allowed(request.headers.get("origin"), host, extra_origins):
            return _error(403, "requests from other sites are refused")
        if request.url.path.startswith("/api/"):
            given = bearer(request.headers.get("authorization"))
            if not token_matches(given, token):
                return _error(401, "missing or wrong token: open the URL `apm serve` printed")
        return await call_next(request)

    for error_type, status in ERROR_STATUS:
        app.add_exception_handler(error_type, _handler(status))

    def graph_and(node: str) -> tuple[Graph, NodeId]:
        graph = load_graph(project)
        return graph, resolve_handle(node, graph.nodes)

    def node_detail(graph: Graph, node_id: NodeId) -> dict[str, Any]:
        states, names = compute_states(graph), handles(graph.nodes)
        return {
            **node_view(graph, states, names, node_id),
            "body": read_body(project.apm_dir, node_id),
            "ancestors": [
                {"id": a, "handle": names[a], "title": graph[a].title}
                for a in reversed(graph.ancestors(node_id))
            ],
            "links": {
                key: [
                    {
                        "id": n,
                        "handle": names[n],
                        "title": graph[n].title,
                        "status": str(graph[n].status),
                    }
                    for n in ids
                ]
                for key, ids in (
                    ("depends_on", graph.dependencies(node_id)),
                    ("dependents", graph.dependents(node_id)),
                    ("waiting_on", states[node_id].waiting_on),
                )
            },
            "history": [asdict(h) for h in node_history(project, node_id)],
            "runs": [asdict(r) for r in store.list_runs(node_id=node_id)],
            "proposals": [
                asdict(p) for p in store.list_proposals(node_id=node_id, status="pending")
            ],
        }

    # --- reading ---

    @app.get("/api/project")
    def get_project() -> dict[str, Any]:
        config = read_config(project.apm_dir)
        graph = load_graph(project)
        return {
            "name": config.name,
            "version": __version__,
            "code_dir": str(project.code_dir),
            "head": rev_parse(project.apm_dir, "HEAD"),
            "agents": [
                {"name": spec.name, "summary": spec.summary}
                for spec in AGENTS.values()
                if spec.user_startable
            ],
            "counts": {
                "nodes": len(graph),
                "ready": len(ready_leaves(graph)),
                "proposals": len(store.list_proposals(status="pending")),
                "running": len(store.list_runs(status="running")),
            },
        }

    @app.get("/api/tree")
    def get_tree() -> dict[str, Any]:
        graph = load_graph(project)
        report = check_all(graph)
        return {
            "head": rev_parse(project.apm_dir, "HEAD"),
            "nodes": all_nodes_view(graph),
            "errors": [asdict(i) for i in report.errors],
            "warnings": [asdict(i) for i in report.warnings],
        }

    @app.get("/api/graph")
    def get_graph(root: str | None = None) -> dict[str, Any]:
        graph = load_graph(project)
        root_id = None if not root else resolve_handle(root, graph.nodes)
        return level_json(graph, root_id)

    @app.get("/api/nodes/{node}")
    def get_node(node: str) -> dict[str, Any]:
        return node_detail(*graph_and(node))

    @app.get("/api/ready")
    def get_ready(root: str | None = None) -> list[dict[str, Any]]:
        graph = load_graph(project)
        root_id = None if not root else resolve_handle(root, graph.nodes)
        states, names = compute_states(graph), handles(graph.nodes)
        return [node_view(graph, states, names, n) for n in ready_leaves(graph, root_id)]

    @app.get("/api/search")
    def get_search(q: str = "") -> list[dict[str, Any]]:
        graph = load_graph(project)
        names = handles(graph.nodes)
        return [
            {"id": h.node_id, "handle": names[h.node_id], "title": h.title, "snippet": h.snippet}
            for h in search(project, graph, q)
        ]

    # --- writing ---

    @app.post("/api/nodes", status_code=201)
    def create_node(body: CreateNode) -> dict[str, Any]:
        graph = load_graph(project)
        new = ops.NewNode(
            title=body.title,
            parent=None if not body.parent else resolve_handle(body.parent, graph.nodes),
            kind=body.kind,
            depends_on=[resolve_handle(d, graph.nodes) for d in body.depends_on],
            body=render_body(intent=body.intent),
        )
        result = ops.create_nodes(project, [new])
        return node_detail(result.graph, result.created[0])

    @app.patch("/api/nodes/{node}")
    def patch_node(node: str, body: PatchNode) -> dict[str, Any]:
        """Each kind of change is its own commit: fields, status, body."""
        _, node_id = graph_and(node)
        if body.title is not None or body.kind is not None:
            ops.update_node(project, node_id, title=body.title, kind=body.kind)
        if body.status is not None:
            ops.set_status(project, node_id, body.status, note=body.note)
        if body.body is not None:
            ops.edit_body(project, node_id, body.body)
        return node_detail(load_graph(project), node_id)

    @app.post("/api/nodes/{node}/move")
    def move_node(node: str, body: MoveNode) -> dict[str, Any]:
        graph, node_id = graph_and(node)
        parent = None if not body.parent else resolve_handle(body.parent, graph.nodes)
        ops.move_node(project, node_id, parent)
        return node_detail(load_graph(project), node_id)

    @app.delete("/api/nodes/{node}")
    def delete_node(node: str, recursive: bool = False) -> dict[str, Any]:
        _, node_id = graph_and(node)
        result = ops.delete_node(project, node_id, recursive=recursive)
        return {"deleted": [n for n in result.nodes if n not in result.graph]}

    @app.post("/api/edges")
    def add_edge(body: Edge) -> dict[str, Any]:
        graph = load_graph(project)
        src, dst = (resolve_handle(h, graph.nodes) for h in (body.src, body.dst))
        return {"changed": ops.link(project, src, dst).sha is not None}

    @app.delete("/api/edges")
    def remove_edge(src: str, dst: str) -> dict[str, Any]:
        graph = load_graph(project)
        a, b = (resolve_handle(h, graph.nodes) for h in (src, dst))
        return {"changed": ops.unlink(project, a, b).sha is not None}

    # --- runs ---

    @app.get("/api/runs")
    def list_runs(node_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        if status not in (None, "running", "succeeded", "failed", "cancelled"):
            return []
        return [asdict(r) for r in store.list_runs(node_id=node_id, status=status)]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, after: int = 0) -> dict[str, Any]:
        run = store.get_run(run_id)
        return {**asdict(run), "events": [asdict(e) for e in store.list_events(run_id, after)]}

    @app.post("/api/runs", status_code=201)
    async def start_run(body: StartRun) -> dict[str, Any]:
        _, node_id = graph_and(body.node_id)
        return asdict(runner.start(body.agent, node_id, body.guidance, critic=body.critic))

    @app.post("/api/runs/{run_id}/continue", status_code=201)
    async def continue_run(run_id: str, body: ContinueRun) -> dict[str, Any]:
        return asdict(runner.continue_run(run_id, body.message))

    @app.delete("/api/runs/{run_id}")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        await runner.cancel(run_id)
        return asdict(store.get_run(run_id))

    # --- proposals ---

    @app.get("/api/proposals")
    def list_proposals(
        node_id: str | None = None, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        if status not in (None, "pending", "accepted", "rejected"):
            return []
        return [asdict(p) for p in store.list_proposals(node_id=node_id, status=status)]

    @app.get("/api/proposals/{proposal_id}")
    def get_proposal(proposal_id: str) -> dict[str, Any]:
        return asdict(store.get_proposal(proposal_id))

    @app.post("/api/proposals/{proposal_id}/accept")
    def accept(proposal_id: str, body: AcceptProposal | None = None) -> dict[str, Any]:
        result = accept_proposal(project, store, proposal_id, body.children if body else None)
        hub.publish({"type": "proposal.resolved", "id": proposal_id, "status": "accepted"})
        return {"created": list(result.created), "sha": result.sha}

    @app.post("/api/proposals/{proposal_id}/reject")
    def reject(proposal_id: str) -> dict[str, Any]:
        reject_proposal(store, proposal_id)
        hub.publish({"type": "proposal.resolved", "id": proposal_id, "status": "rejected"})
        return {"id": proposal_id, "status": "rejected"}

    # --- live events ---

    @app.websocket("/ws")
    async def events(socket: WebSocket) -> None:
        host = socket.headers.get("host")
        allowed = (
            host_is_loopback(host)
            and origin_allowed(socket.headers.get("origin"), host, extra_origins)
            and token_matches(socket.query_params.get("token"), token)
        )
        if not allowed:
            await socket.close(code=1008)
            return
        await socket.accept()
        queue = hub.subscribe()
        try:
            await socket.send_json({"type": "hello", "head": rev_parse(project.apm_dir, "HEAD")})
            while True:
                await socket.send_json(await queue.get())
        except WebSocketDisconnect, RuntimeError:
            pass
        finally:
            hub.unsubscribe(queue)

    # --- the UI ---

    if static_dir is not None and (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    else:

        @app.get("/", response_class=HTMLResponse)
        def ui_missing() -> str:
            return UI_MISSING

    return app


async def _watch_head(project: Project, hub: Hub) -> None:
    """Announces every new plan commit, whoever made it: this server, the CLI, or an agent
    through the MCP.
    """
    last = rev_parse(project.apm_dir, "HEAD")
    while True:
        await asyncio.sleep(WATCH_INTERVAL_S)
        with contextlib.suppress(GitError):
            head = await asyncio.to_thread(rev_parse, project.apm_dir, "HEAD")
            if head != last:
                last = head
                hub.publish({"type": "graph.changed", "head": head})


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _handler(status: int) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handle(_: Request, err: Exception) -> Response:
        message = err.args[0] if isinstance(err, KeyError) and err.args else str(err)
        return _error(status, str(message))

    return handle
