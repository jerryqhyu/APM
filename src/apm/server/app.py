"""The HTTP and WebSocket API (plans.md §6.3). The web UI is its only intended client."""

import asyncio
import contextlib
import re
import shutil
import tempfile
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from apm import __version__
from apm.core import ops
from apm.core.archive import SUFFIX, export_archive
from apm.core.config import read_config
from apm.core.errors import ApmError
from apm.core.files import read_body, render_body
from apm.core.git import GitError, rev_parse
from apm.core.graph import Graph
from apm.core.lifting import check_all
from apm.core.model import Kind, NodeId, Status
from apm.core.mutate import commit_hand_edits
from apm.core.project import Project, load_graph
from apm.core.proposals import accept_proposal, close_orphaned, reject_proposal
from apm.core.reconcile import Report, describe, reconcile
from apm.core.store import PROJECT, Store
from apm.core.views import all_nodes_view, level_json, node_history, node_ref, node_view, search
from apm.runner.agents import AGENTS, MODES
from apm.runner.runner import Runner
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


class MergeNodes(BaseModel):
    target: str
    others: list[str]
    title: str | None = None


class Edge(BaseModel):
    src: str
    dst: str


class StartRun(BaseModel):
    agent: str
    node_id: str | None = None
    """None = the project as a whole (the chat only)."""
    message: str = ""
    critic: bool = True
    mode: str | None = None


class ContinueRun(BaseModel):
    message: str
    mode: str | None = None


class AnswerRequest(BaseModel):
    allow: bool
    message: str = ""
    answers: dict[str, str] | None = None


class AcceptProposal(BaseModel):
    children: list[dict[str, Any]] | None = None
    """The children to create, if you edited or trimmed the proposal. Default: all of them."""


class Hub:
    """Fan-out of live events to every connected WebSocket."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        """Safe to call from any thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: dict[str, Any]) -> None:
        for queue in self._queues:
            # A client too slow to keep up misses events rather than stalling the others; it
            # refetches on the next one it does get.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)


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

    @app.exception_handler(ApmError)
    async def user_error(_: Request, err: ApmError) -> Response:
        return _error(err.http_status, str(err))

    @app.exception_handler(KeyError)
    async def not_found(_: Request, err: KeyError) -> Response:
        """An unknown run or proposal id."""
        return _error(404, str(err.args[0]) if err.args else "not found")

    def locate(node: str) -> tuple[Graph, NodeId]:
        graph = load_graph(project)
        return graph, graph.resolve(node)

    def node_detail(graph: Graph, node_id: NodeId) -> dict[str, Any]:
        state = graph.states[node_id]
        return {
            **node_view(graph, node_id),
            "body": read_body(project.apm_dir, node_id),
            "ancestors": [node_ref(graph, a) for a in reversed(graph.ancestors(node_id))],
            "links": {
                "depends_on": [node_ref(graph, n) for n in graph.dependencies(node_id)],
                "dependents": [node_ref(graph, n) for n in graph.dependents(node_id)],
                "waiting_on": [node_ref(graph, n) for n in state.waiting_on],
                "children": [node_ref(graph, n) for n in graph.children(node_id)],
            },
            "history": [asdict(h) for h in node_history(project, node_id)],
            "proposals": [
                asdict(p) for p in store.list_proposals(node_id=node_id, status="pending")
            ],
        }

    def run_view(run_id: str, after: int = 0) -> dict[str, Any]:
        return {
            **asdict(store.get_run(run_id)),
            "events": [asdict(e) for e in store.list_events(run_id, after)],
            "requests": runner.pending_requests(run_id),
        }

    # --- reading ---

    @app.get("/api/project")
    def get_project() -> dict[str, Any]:
        config = read_config(project.apm_dir)
        graph = load_graph(project)
        close_orphaned(store, graph)
        return {
            "name": config.name,
            "version": __version__,
            "code_dir": str(project.code_dir),
            "head": rev_parse(project.apm_dir, "HEAD"),
            "agents": [
                {
                    "name": spec.name,
                    "label": spec.label,
                    "summary": spec.summary,
                    "needs_node": spec.needs_node,
                }
                for spec in AGENTS.values()
                if spec.user_startable
            ],
            "modes": [{"name": name, "label": label} for name, label in MODES.items()],
            "default_mode": config.runner.permission_mode,
            "counts": {
                "nodes": len(graph),
                "ready": len(graph.ready_leaves()),
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
        return level_json(graph, graph.resolve(root) if root else None)

    @app.get("/api/nodes/{node}")
    def get_node(node: str) -> dict[str, Any]:
        return node_detail(*locate(node))

    @app.get("/api/ready")
    def get_ready(root: str | None = None) -> list[dict[str, Any]]:
        graph = load_graph(project)
        ready = graph.ready_leaves(graph.resolve(root) if root else None)
        return [node_view(graph, n) for n in ready]

    @app.get("/api/search")
    def get_search(q: str = "") -> list[dict[str, Any]]:
        graph = load_graph(project)
        return [
            {**node_ref(graph, hit.node_id), "snippet": hit.snippet}
            for hit in search(project, graph, q)
        ]

    @app.get("/api/export")
    def get_export(with_runs: bool = False) -> FileResponse:
        """The project as an `.apm.zip` download, like `apm export`.

        With `with_runs`, run transcripts go in too, and they can hold secrets: the UI warns first.
        """
        scratch = Path(tempfile.mkdtemp(prefix="apm-download-"))
        try:
            manifest = export_archive(project, scratch / f"plan{SUFFIX}", with_runs=with_runs)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        return FileResponse(
            scratch / f"plan{SUFFIX}",
            media_type="application/zip",
            filename=archive_name(manifest["project"]),
            background=BackgroundTask(shutil.rmtree, scratch, ignore_errors=True),
        )

    # --- changing the plan ---

    @app.post("/api/nodes", status_code=201)
    def create_node(body: CreateNode) -> dict[str, Any]:
        graph = load_graph(project)
        new = ops.NewNode(
            title=body.title,
            parent=graph.resolve(body.parent) if body.parent else None,
            kind=body.kind,
            depends_on=[graph.resolve(d) for d in body.depends_on],
            body=render_body(intent=body.intent),
        )
        result = ops.create_nodes(project, [new])
        return node_detail(result.graph, result.created[0])

    @app.patch("/api/nodes/{node}")
    def patch_node(node: str, body: PatchNode) -> dict[str, Any]:
        """Each kind of change is its own commit: fields, status, body."""
        _, node_id = locate(node)
        if body.title is not None or body.kind is not None:
            ops.update_node(project, node_id, title=body.title, kind=body.kind)
        if body.status is not None:
            ops.set_status(project, node_id, body.status, note=body.note)
        if body.body is not None:
            ops.edit_body(project, node_id, body.body)
        return node_detail(load_graph(project), node_id)

    @app.post("/api/nodes/{node}/move")
    def move_node(node: str, body: MoveNode) -> dict[str, Any]:
        graph, node_id = locate(node)
        ops.move_node(project, node_id, graph.resolve(body.parent) if body.parent else None)
        return node_detail(load_graph(project), node_id)

    @app.delete("/api/nodes/{node}")
    def delete_node(node: str, recursive: bool = False) -> dict[str, Any]:
        _, node_id = locate(node)
        result = ops.delete_node(project, node_id, recursive=recursive)
        return {"deleted": [n for n in result.nodes if n not in result.graph]}

    @app.post("/api/merge")
    def merge(body: MergeNodes) -> dict[str, Any]:
        graph, target = locate(body.target)
        result = ops.merge_nodes(
            project, target, [graph.resolve(o) for o in body.others], title=body.title
        )
        return node_detail(result.graph, target)

    @app.post("/api/edges")
    def add_edge(body: Edge) -> dict[str, Any]:
        graph = load_graph(project)
        result = ops.link(project, graph.resolve(body.src), graph.resolve(body.dst))
        return {"changed": result.sha is not None}

    @app.delete("/api/edges")
    def remove_edge(src: str, dst: str) -> dict[str, Any]:
        graph = load_graph(project)
        result = ops.unlink(project, graph.resolve(src), graph.resolve(dst))
        return {"changed": result.sha is not None}

    @app.post("/api/reconcile")
    def run_reconcile(retarget: bool = False) -> dict[str, Any]:
        report = reconcile(project, retarget=retarget)
        return {
            "done": describe(Report(done=report.done)),
            "findings": describe(Report(findings=report.findings)),
        }

    @app.post("/api/commit")
    def commit_edits() -> dict[str, Any]:
        """Commits edits made by hand in the plan repo."""
        result = commit_hand_edits(project)
        return {"sha": result.sha, "nodes": list(result.nodes)}

    # --- runs: one turn of an agent ---

    @app.get("/api/runs")
    def list_runs(
        node_id: str | None = None, scope: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """`scope=project` lists the runs that are about the project as a whole."""
        if status not in (None, "running", "succeeded", "failed", "cancelled"):
            return []
        where = PROJECT if scope == "project" else node_id
        return [asdict(r) for r in store.list_runs(node_id=where, status=status, limit=500)]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, after: int = 0) -> dict[str, Any]:
        return run_view(run_id, after)

    @app.post("/api/runs", status_code=201)
    async def start_run(body: StartRun) -> dict[str, Any]:
        node_id = locate(body.node_id)[1] if body.node_id else None
        run = runner.start(body.agent, node_id, body.message, critic=body.critic, mode=body.mode)
        return asdict(run)

    @app.post("/api/runs/{run_id}/continue", status_code=201)
    async def continue_run(run_id: str, body: ContinueRun) -> dict[str, Any]:
        return asdict(runner.continue_run(run_id, body.message, mode=body.mode))

    @app.delete("/api/runs/{run_id}")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        await runner.cancel(run_id)
        return asdict(store.get_run(run_id))

    @app.get("/api/requests")
    def list_requests() -> list[dict[str, Any]]:
        """Everything a running agent is waiting on you for."""
        return runner.pending_requests()

    @app.post("/api/requests/{request_id}")
    async def answer_request(request_id: str, body: AnswerRequest) -> dict[str, Any]:
        await runner.answer(
            request_id, allow=body.allow, message=body.message, answers=body.answers
        )
        return {"answered": request_id}

    # --- proposals ---

    @app.get("/api/proposals")
    def list_proposals(
        node_id: str | None = None, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        if status not in (None, "pending", "accepted", "rejected"):
            return []
        close_orphaned(store, load_graph(project))
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


def archive_name(project_name: str) -> str:
    """`<project>.apm.zip`, with anything unsafe in a file name replaced by a dash."""
    stem = re.sub(r"[^\w.-]+", "-", project_name).strip(".-") or "plan"
    return f"{stem}{SUFFIX}"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)
