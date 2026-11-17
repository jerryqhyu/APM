"""The `apm` command (plans.md §10)."""

import asyncio
import functools
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from apm import __version__
from apm.core import ops
from apm.core.errors import ApmError
from apm.core.files import read_body, render_body
from apm.core.git import git
from apm.core.graph import Graph
from apm.core.lifting import check_all
from apm.core.model import Kind, NodeId, Status
from apm.core.mutate import Mutation, OpError, commit_hand_edits
from apm.core.project import (
    LINK_FILE,
    Project,
    ProjectError,
    find_project,
    init_project,
    load_graph,
)
from apm.core.proposals import accept_proposal, parse_decomposition, reject_proposal
from apm.core.reconcile import describe, reconcile
from apm.core.store import Proposal, Store
from apm.core.views import all_nodes_view, node_line, node_text, node_view, outline, search

HELP = {"help_option_names": ["-h", "--help"]}
app = typer.Typer(name="apm", no_args_is_help=True, add_completion=False, context_settings=HELP)
node_app = typer.Typer(no_args_is_help=True, help="Work with nodes.", context_settings=HELP)
proposal_app = typer.Typer(
    no_args_is_help=True, help="Review what agents proposed.", context_settings=HELP
)
app.add_typer(node_app, name="node")
app.add_typer(proposal_app, name="proposal")

Json = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def command[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Turns the errors a user can cause into a one-line message and exit code 1."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except ApmError as err:
            for line in str(err).splitlines():
                typer.echo(f"error: {line}", err=True)
            raise typer.Exit(1) from None

    return wrapper


def plan() -> tuple[Project, Graph]:
    """The project you are in, and its plan as it is now."""
    project = find_project()
    return project, load_graph(project)


def locate(handle: str) -> tuple[Project, Graph, NodeId]:
    """The project you are in, its plan, and the node a handle names."""
    project, graph = plan()
    return project, graph, graph.resolve(handle)


def print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


def report(result: Mutation, text: str) -> None:
    """Says what an operation did, or that it changed nothing."""
    typer.echo(text if result.sha else "nothing to change")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", "-v", callback=_print_version, is_eager=True, help="Print the version."
        ),
    ] = False,
) -> None:
    """A local-first project manager built for working with AI agents."""


# --- the project --------------------------------------------------------------------------------


@app.command()
@command
def init(
    name: Annotated[
        str | None, typer.Option(help="Project name. Default: the folder name.")
    ] = None,
) -> None:
    """Create the plan repo (.apm/) for the code in this folder."""
    project = init_project(Path.cwd(), name=name)
    typer.echo(f"created {project.apm_dir}")
    typer.echo('next: apm serve, or apm node add "My first idea"')


@app.command()
@command
def check() -> None:
    """Validate the plan: errors (exit code 1) and warnings."""
    result = check_all(plan()[1])
    for issue in result.errors:
        typer.echo(f"error {issue.code}: {issue.message}")
    for issue in result.warnings:
        typer.echo(f"warning {issue.code}: {issue.message}")
    if not result.errors and not result.warnings:
        typer.echo("ok")
    if result.errors:
        raise typer.Exit(1)


@app.command()
@command
def commit() -> None:
    """Validate and commit edits you made by hand in .apm/."""
    result = commit_hand_edits(find_project())
    report(result, f"committed {len(result.nodes)} node(s)")


@app.command("reconcile")
@command
def reconcile_() -> None:
    """Mark in-progress nodes done when their pull request or commits have merged."""
    lines = describe(reconcile(find_project()))
    for line in lines:
        typer.echo(line)
    if not lines:
        typer.echo("nothing to reconcile")


@app.command()
@command
def ready(
    root_: Annotated[str | None, typer.Option("--root", help="Only under this node.")] = None,
    as_json: Json = False,
) -> None:
    """List what can be worked on now: ready leaves."""
    _, graph = plan()
    ids = graph.ready_leaves(graph.resolve(root_) if root_ else None)
    if as_json:
        print_json([node_view(graph, n) for n in ids])
        return
    for node_id in ids:
        typer.echo(node_line(graph, node_id))


@app.command("search")
@command
def search_(query: str, as_json: Json = False) -> None:
    """Search titles and bodies."""
    project, graph = plan()
    hits = search(project, graph, query)
    if as_json:
        print_json([{"id": h.node_id, "title": h.title, "snippet": h.snippet} for h in hits])
        return
    for hit in hits:
        tail = f"  — {hit.snippet}" if hit.snippet else ""
        typer.echo(f"{graph.handles[hit.node_id]} {hit.title}{tail}")


@app.command("link-dep")
@command
def link_dep(src: str, dst: str) -> None:
    """Record that SRC depends on DST."""
    project, graph = plan()
    report(ops.link(project, graph.resolve(src), graph.resolve(dst)), "linked")


@app.command("unlink-dep")
@command
def unlink_dep(src: str, dst: str) -> None:
    """Remove the dependency of SRC on DST."""
    project, graph = plan()
    report(ops.unlink(project, graph.resolve(src), graph.resolve(dst)), "unlinked")


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio. Claude Code starts this; you rarely run it yourself."""
    from apm.mcp.server import main as serve_mcp  # keeps `apm --help` fast

    serve_mcp()


@app.command("plugin-path")
def plugin_path() -> None:
    """Print where the Claude Code plugin is: `claude --plugin-dir "$(apm plugin-path)"`."""
    typer.echo(Path(__file__).parent / "plugin")


# --- the plan repo's remote ---------------------------------------------------------------------


@app.command()
@command
def link(remote: Annotated[str, typer.Option(help="URL of the plan repo's remote.")]) -> None:
    """Set the plan repo's remote and record it in .apm-link."""
    project = find_project()
    remotes = git(project.apm_dir, ["remote"]).split()
    action = ["set-url"] if "origin" in remotes else ["add"]
    git(project.apm_dir, ["remote", *action, "origin", remote])
    link_file = project.code_dir / LINK_FILE
    data = json.loads(link_file.read_text(encoding="utf-8"))
    link_file.write_text(json.dumps({**data, "remote": remote}) + "\n", encoding="utf-8")
    typer.echo(f"plan repo remote: {remote}")


@app.command()
@command
def clone() -> None:
    """Clone the plan repo named in .apm-link (for a fresh checkout of the code)."""
    here = Path.cwd().resolve()
    code_dir = next((d for d in (here, *here.parents) if (d / LINK_FILE).is_file()), None)
    if code_dir is None:
        raise ProjectError(f"no {LINK_FILE} at or above {here}")
    data = json.loads((code_dir / LINK_FILE).read_text(encoding="utf-8"))
    if not data.get("remote"):
        raise ProjectError(f"{LINK_FILE} names no remote (set one with `apm link --remote <url>`)")
    target = code_dir / data["path"]
    if target.exists():
        raise ProjectError(f"{target} already exists")
    git(code_dir, ["clone", "--quiet", data["remote"], str(target)])
    typer.echo(f"cloned {data['remote']} into {target}")


@app.command()
@command
def push() -> None:
    """Push the plan repo."""
    typer.echo(git(find_project().apm_dir, ["push", "--quiet", "-u", "origin", "HEAD"]) or "pushed")


@app.command()
@command
def pull() -> None:
    """Pull the plan repo (fast-forward only)."""
    typer.echo(git(find_project().apm_dir, ["pull", "--quiet", "--ff-only"]) or "up to date")


# --- nodes --------------------------------------------------------------------------------------


@node_app.command("add")
@command
def node_add(
    title: str,
    parent: Annotated[str | None, typer.Option("--parent", "-p", help="Parent node.")] = None,
    kind: Annotated[Kind, typer.Option(help="work or decision.")] = Kind.WORK,
    dep: Annotated[list[str] | None, typer.Option("--dep", help="A node this depends on.")] = None,
    intent: Annotated[str, typer.Option(help="Why it exists: goes in the body.")] = "",
) -> None:
    """Add a node."""
    project, graph = plan()
    new = ops.NewNode(
        title=title,
        parent=graph.resolve(parent) if parent else None,
        kind=kind,
        depends_on=[graph.resolve(d) for d in dep or []],
        body=render_body(intent=intent),
    )
    result = ops.create_nodes(project, [new])
    typer.echo(f"{result.graph.handles[result.created[0]]} {title}")


@node_app.command("ls")
@command
def node_ls(
    root_: Annotated[str | None, typer.Option("--root", help="Only under this node.")] = None,
    depth: Annotated[int | None, typer.Option(help="How many levels to show.")] = None,
    as_json: Json = False,
) -> None:
    """Show the plan as an outline."""
    _, graph = plan()
    scope = graph.resolve(root_) if root_ else None
    if as_json:
        inside = None if scope is None else set(graph.descendants(scope))
        print_json([n for n in all_nodes_view(graph) if inside is None or n["id"] in inside])
        return
    typer.echo(outline(graph, scope, depth) or '(no nodes yet: apm node add "...")')


@node_app.command("show")
@command
def node_show(handle: str, as_json: Json = False) -> None:
    """Show one node: its state, links and body."""
    project, graph, node_id = locate(handle)
    if as_json:
        print_json({**node_view(graph, node_id), "body": read_body(project.apm_dir, node_id)})
        return
    typer.echo(node_text(project, graph, node_id))


@node_app.command("set")
@command
def node_set(
    handle: str,
    title: Annotated[str | None, typer.Option(help="New title.")] = None,
    kind: Annotated[Kind | None, typer.Option(help="work or decision.")] = None,
) -> None:
    """Change a node's title or kind."""
    project, _, node_id = locate(handle)
    report(ops.update_node(project, node_id, title=title, kind=kind), "updated")


@node_app.command("status")
@command
def node_status(
    handle: str,
    status: Status,
    note: Annotated[str, typer.Option(help="Why: goes in the commit message.")] = "",
) -> None:
    """Set a node's status: todo, in_progress, done or dropped."""
    project, _, node_id = locate(handle)
    report(ops.set_status(project, node_id, status, note=note), f"{handle}: {status}")


@node_app.command("mv")
@command
def node_mv(
    handle: str,
    parent: Annotated[str | None, typer.Argument(help="The new parent.")] = None,
    top: Annotated[bool, typer.Option("--top", help="Move to the top level.")] = False,
) -> None:
    """Move a node, with everything under it, to another parent."""
    if (parent is None) != top:
        raise OpError("give a new parent, or --top")
    project, graph, node_id = locate(handle)
    target = graph.resolve(parent) if parent else None
    report(ops.move_node(project, node_id, target), "moved")


@node_app.command("merge")
@command
def node_merge(
    target: Annotated[str, typer.Argument(help="The node that survives.")],
    others: Annotated[list[str], typer.Argument(help="Nodes folded into it, then deleted.")],
    title: Annotated[str | None, typer.Option(help="New title for the merged node.")] = None,
) -> None:
    """Merge several nodes into one: children, dependencies and bodies move to the target."""
    project, graph, node_id = locate(target)
    result = ops.merge_nodes(project, node_id, [graph.resolve(o) for o in others], title=title)
    report(result, f"merged {len(others) + 1} nodes into {target}")


@node_app.command("rm")
@command
def node_rm(
    handle: str,
    recursive: Annotated[
        bool, typer.Option("--recursive", "-r", help="Also delete everything under it.")
    ] = False,
) -> None:
    """Delete a node and the edges that point at it."""
    project, _, node_id = locate(handle)
    report(ops.delete_node(project, node_id, recursive=recursive), "deleted")


@node_app.command("edit")
@command
def node_edit(handle: str) -> None:
    """Edit a node's body in $EDITOR and commit it."""
    project, _, node_id = locate(handle)
    before = read_body(project.apm_dir, node_id) or render_body()
    report(ops.edit_body(project, node_id, edit_text(before, suffix=f"-{handle}.md")), "saved")


def edit_text(text: str, suffix: str = ".md") -> str:
    """Opens `text` in the user's editor and returns what they saved."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"apm{suffix}"
        path.write_text(text, encoding="utf-8")
        try:
            subprocess.run([*shlex.split(editor), str(path)], check=True)
        except (OSError, subprocess.CalledProcessError) as err:
            raise OpError(f"editor failed ({editor}): {err}") from None
        return path.read_text(encoding="utf-8")


# --- the server ---------------------------------------------------------------------------------


@app.command()
@command
def serve(
    port: Annotated[
        int, typer.Option(help="Port to listen on. If taken, a free one is used.")
    ] = 4100,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the UI.")] = True,
    dev: Annotated[
        bool, typer.Option(hidden=True, help="Also accept the Vite dev server.")
    ] = False,
) -> None:
    """Start the web UI on this machine only."""
    import uvicorn  # only this command needs the server stack

    from apm.server.app import create_app
    from apm.server.security import load_or_create_token, write_server_file

    project = find_project()
    token = load_or_create_token(project.apm_dir)
    port = free_port(port)
    write_server_file(project.apm_dir, token=token, port=port)
    origins = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"}) if dev else frozenset()
    url = f"http://127.0.0.1:{port}/?token={token}"
    for line in describe(reconcile(project)):
        typer.echo(line)
    typer.echo(f"APM is running at {url}")
    typer.echo("Only this machine can reach it. Press Ctrl+C to stop.")
    if open_browser:
        threading.Thread(target=open_when_listening, args=(port, url), daemon=True).start()
    uvicorn.run(
        create_app(project, token, extra_origins=origins),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


def open_when_listening(port: int, url: str, timeout: float = 15.0) -> None:
    """Opens the browser once the server accepts connections, so the page never loads too early."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.1)


def free_port(preferred: int) -> int:
    """`preferred` if nothing is listening there, otherwise a port the system picks."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return probe.getsockname()[1]
    raise OpError("no free port to listen on")


# --- agents -------------------------------------------------------------------------------------


@app.command()
@command
def run(
    agent: Annotated[str, typer.Argument(help="assistant, decomposer or implementer.")],
    handle: Annotated[
        str | None, typer.Argument(help="The node. Omit to chat about the whole project.")
    ] = None,
    message: Annotated[
        str, typer.Option("--message", "-m", "--guidance", "-g", help="What you want from it.")
    ] = "",
    critic: Annotated[
        bool, typer.Option("--critic/--no-critic", help="Review the breakdown.")
    ] = True,
    mode: Annotated[
        str | None,
        typer.Option(help="Permission mode: auto, manual, acceptEdits, plan or dontAsk."),
    ] = None,
) -> None:
    """Run an agent and follow it here. It asks you in the terminal when it needs an answer."""
    from apm.runner.runner import Runner
    from apm.runner.transcript import format_event

    project, graph = plan()
    node_id = graph.resolve(handle) if handle else None
    name = agent if agent.startswith("apm-") else f"apm-{agent}"
    store = Store(project.apm_dir)
    failed: list[dict[str, Any]] = []
    waiting: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def show(event: dict[str, Any]) -> None:
        if event["type"] == "run.started":
            typer.echo(f"▶ {event['run']['agent']} (run {event['run']['id']})")
        elif event["type"] == "run.event":
            for line in format_event(event["event"]["data"]):
                typer.echo(line)
        elif event["type"] == "run.request":
            waiting.put_nowait(event["request"])
        elif event["type"] == "run.finished" and event["run"]["status"] != "succeeded":
            failed.append(event["run"])
            typer.echo(f"✗ {event['run']['status']}: {event['run']['error'] or ''}", err=True)
        elif event["type"] == "proposal.created":
            proposal = event["proposal"]
            typer.echo(f"proposal {proposal['id']}: {len(proposal['data']['children'])} children")
            typer.echo(f"next: apm proposal show {proposal['id']}")

    async def go() -> None:
        runner = Runner(project, store, show)

        async def answer_requests() -> None:
            while True:
                request = await waiting.get()
                allow, answers = await asyncio.to_thread(ask_in_terminal, request)
                await runner.answer(request["id"], allow=allow, answers=answers)

        asker = asyncio.create_task(answer_requests())
        try:
            started = runner.start(name, node_id, message, critic=critic, mode=mode)
            await runner.wait(started.id)
        finally:
            asker.cancel()
            await runner.shutdown()

    asyncio.run(go())
    if failed:
        raise typer.Exit(1)


def ask_in_terminal(request: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
    """Puts an agent's question or permission prompt to the person at the terminal."""
    if request["kind"] != "question":
        what = request["input"].get("command") or json.dumps(request["input"], ensure_ascii=False)
        why = f"  ({request['description']})" if request["description"] else ""
        typer.echo(f"? {request['tool']}: {what}{why}")
        return typer.confirm("  allow?", default=False), None
    answers = {}
    for question in request["input"].get("questions", []):
        options = question.get("options", [])
        typer.echo(f"? {question['question']}")
        for number, option in enumerate(options, start=1):
            typer.echo(f"  {number}. {option['label']}")
        choice = typer.prompt("  your choice (a number, or your own words)", default="1")
        picked = int(choice) - 1 if choice.isdigit() and 0 < int(choice) <= len(options) else None
        answers[question["question"]] = options[picked]["label"] if picked is not None else choice
    return True, answers


# --- proposals ----------------------------------------------------------------------------------


@proposal_app.command("ls")
@command
def proposal_ls(
    everything: Annotated[bool, typer.Option("--all", help="Include resolved ones.")] = False,
) -> None:
    """List proposals waiting for your decision."""
    project, graph = plan()
    proposals = Store(project.apm_dir).list_proposals(status=None if everything else "pending")
    for p in proposals:
        target = graph.handles.get(p.node_id, "(deleted)")
        typer.echo(f"{p.id} [{p.status}] {len(p.data.get('children', []))} children for {target}")
    if not proposals:
        typer.echo("no proposals")


@proposal_app.command("show")
@command
def proposal_show(proposal_id: str) -> None:
    """Show a proposal: the children it would create, and the review."""
    project, graph = plan()
    proposal = _proposal(project, proposal_id)
    data = parse_decomposition(proposal.data)
    target = graph[proposal.node_id].title if proposal.node_id in graph else "(deleted)"
    typer.echo(f"proposal {proposal.id} [{proposal.status}] for: {target}")
    if data.rationale:
        typer.echo(f"\n{data.rationale}")
    for child in data.children:
        needs = [*child.depends_on_keys, *child.depends_on_ids]
        arrow = f" → {', '.join(needs)}" if needs else ""
        typer.echo(f"\n[{child.key}] {child.title} ({child.kind}){arrow}")
        if child.intent:
            typer.echo(f"    {child.intent}")
        for criterion in child.acceptance:
            typer.echo(f"    - {criterion}")
    for warning in data.warnings:
        typer.echo(f"\nwarning: {warning}")
    if proposal.review:
        review = proposal.review
        revised = " (revised once)" if review.get("revised") else ""
        typer.echo(f"\nreview: {review.get('verdict')}{revised}. {review.get('summary', '')}")
        for issue in review.get("issues", []):
            where = f"[{issue['child_key']}] " if issue.get("child_key") else ""
            typer.echo(f"    {issue.get('severity')}: {where}{issue.get('problem')}")


@proposal_app.command("accept")
@command
def proposal_accept(
    proposal_id: str,
    only: Annotated[list[str] | None, typer.Option("--only", help="Keep only these keys.")] = None,
) -> None:
    """Accept a proposal: its children become nodes, in one commit."""
    project = find_project()
    children = None
    if only:
        proposal = _proposal(project, proposal_id)
        children = [c for c in proposal.data.get("children", []) if c.get("key") in only]
    result = accept_proposal(project, Store(project.apm_dir), proposal_id, children)
    for node_id in result.created:
        typer.echo(f"{result.graph.handles[node_id]} {result.graph[node_id].title}")


@proposal_app.command("reject")
@command
def proposal_reject(proposal_id: str) -> None:
    """Reject a proposal. Nothing is written to the plan."""
    reject_proposal(Store(find_project().apm_dir), proposal_id)
    typer.echo("rejected")


def _proposal(project: Project, proposal_id: str) -> Proposal:
    try:
        return Store(project.apm_dir).get_proposal(proposal_id)
    except KeyError:
        raise OpError(f"no proposal {proposal_id} (see `apm proposal ls --all`)") from None


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
