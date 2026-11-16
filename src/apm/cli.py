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
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from apm import __version__
from apm.core import ops
from apm.core.body import read_body, render_body
from apm.core.config import ConfigError
from apm.core.git import GitError, git
from apm.core.graph import Graph
from apm.core.handedit import commit_hand_edits
from apm.core.ids import HandleError, handles, resolve_handle
from apm.core.lifting import check_all
from apm.core.lock import LockTimeoutError
from apm.core.model import Kind, NodeId, Status
from apm.core.mutate import DirtyPlanError, Mutation, OpError
from apm.core.ndjson import GraphParseError, InvalidNodeError
from apm.core.outline import format_node_line, format_outline
from apm.core.project import (
    LINK_FILE,
    Project,
    ProjectError,
    find_project,
    init_project,
    load_graph,
)
from apm.core.proposals import (
    ProposalError,
    ProposalResolvedError,
    accept_proposal,
    parse_decomposition,
    reject_proposal,
)
from apm.core.reconcile import describe, reconcile
from apm.core.search import search
from apm.core.status import compute_states, ready_leaves
from apm.core.store import Proposal, Store
from apm.core.validate import InvariantError
from apm.core.views import all_nodes_view, node_view
from apm.runner.agents import RunRefused

HELP = {"help_option_names": ["-h", "--help"]}
app = typer.Typer(name="apm", no_args_is_help=True, add_completion=False, context_settings=HELP)
node_app = typer.Typer(no_args_is_help=True, help="Work with nodes.", context_settings=HELP)
app.add_typer(node_app, name="node")

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
    ProposalError,
    ProposalResolvedError,
)

Json = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def command[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Turns the errors a user can cause into a one-line message and exit code 1."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except EXPECTED_ERRORS as err:
            for line in str(err).splitlines():
                typer.echo(f"error: {line}", err=True)
            raise typer.Exit(1) from None

    return wrapper


def resolve(graph: Graph, value: str) -> NodeId:
    return resolve_handle(value, graph.nodes)


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


# --- project ------------------------------------------------------------------------------------


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
    typer.echo('next: apm node add "My first idea"')


@app.command()
@command
def check() -> None:
    """Validate the plan: errors (exit code 1) and warnings."""
    report_ = check_all(load_graph(find_project()))
    for issue in report_.errors:
        typer.echo(f"error {issue.code}: {issue.message}")
    for issue in report_.warnings:
        typer.echo(f"warning {issue.code}: {issue.message}")
    if not report_.errors and not report_.warnings:
        typer.echo("ok")
    if report_.errors:
        raise typer.Exit(1)


@app.command()
@command
def commit() -> None:
    """Validate and commit edits you made by hand in .apm/."""
    result = commit_hand_edits(find_project())
    report(result, f"committed {len(result.nodes)} node(s)")


@app.command()
@command
def ready(
    root_: Annotated[str | None, typer.Option("--root", help="Only under this node.")] = None,
    as_json: Json = False,
) -> None:
    """List what can be worked on now: ready leaves."""
    graph = load_graph(find_project())
    scope = None if root_ is None else resolve(graph, root_)
    ids = ready_leaves(graph, scope)
    states, names = compute_states(graph), handles(graph.nodes)
    if as_json:
        print_json([node_view(graph, states, names, n) for n in ids])
        return
    for node_id in ids:
        typer.echo(format_node_line(graph, node_id, states, names))


@app.command("reconcile")
@command
def reconcile_() -> None:
    """Mark in-progress nodes done when their pull request or commits have merged."""
    lines = describe(reconcile(find_project()))
    for line in lines:
        typer.echo(line)
    if not lines:
        typer.echo("nothing to reconcile")


@app.command("search")
@command
def search_(query: str, as_json: Json = False) -> None:
    """Search titles and bodies."""
    project = find_project()
    graph = load_graph(project)
    hits = search(project, graph, query)
    names = handles(graph.nodes)
    if as_json:
        print_json([{"id": h.node_id, "title": h.title, "snippet": h.snippet} for h in hits])
        return
    for hit in hits:
        typer.echo(
            f"{names[hit.node_id]} {hit.title}" + (f"  — {hit.snippet}" if hit.snippet else "")
        )


@app.command("link-dep")
@command
def link_dep(src: str, dst: str) -> None:
    """Record that SRC depends on DST."""
    project = find_project()
    graph = load_graph(project)
    result = ops.link(project, resolve(graph, src), resolve(graph, dst))
    report(result, "linked")


@app.command("unlink-dep")
@command
def unlink_dep(src: str, dst: str) -> None:
    """Remove the dependency of SRC on DST."""
    project = find_project()
    graph = load_graph(project)
    result = ops.unlink(project, resolve(graph, src), resolve(graph, dst))
    report(result, "unlinked")


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio. Claude Code starts this; you rarely run it yourself."""
    from apm.mcp.server import main as serve_mcp

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
    project = find_project()
    graph = load_graph(project)
    new = ops.NewNode(
        title=title,
        parent=None if parent is None else resolve(graph, parent),
        kind=kind,
        depends_on=[resolve(graph, d) for d in dep or []],
        body=render_body(intent=intent),
    )
    result = ops.create_nodes(project, [new])
    typer.echo(f"{handles(result.graph.nodes)[result.created[0]]} {title}")


@node_app.command("ls")
@command
def node_ls(
    root_: Annotated[str | None, typer.Option("--root", help="Only under this node.")] = None,
    depth: Annotated[int | None, typer.Option(help="How many levels to show.")] = None,
    as_json: Json = False,
) -> None:
    """Show the plan as an outline."""
    graph = load_graph(find_project())
    scope = None if root_ is None else resolve(graph, root_)
    if as_json:
        inside = None if scope is None else set(graph.descendants(scope))
        print_json([n for n in all_nodes_view(graph) if inside is None or n["id"] in inside])
        return
    typer.echo(format_outline(graph, scope, depth) or '(no nodes yet: apm node add "...")')


@node_app.command("show")
@command
def node_show(handle: str, as_json: Json = False) -> None:
    """Show one node: its state, links and body."""
    project = find_project()
    graph = load_graph(project)
    node_id = resolve(graph, handle)
    states, names = compute_states(graph), handles(graph.nodes)
    body = read_body(project.apm_dir, node_id)
    if as_json:
        print_json({**node_view(graph, states, names, node_id), "body": body})
        return
    node = graph[node_id]
    typer.echo(format_node_line(graph, node_id, states, names))
    typer.echo(f"id: {node_id}")
    for label, ids in (
        ("parent", [node.parent] if node.parent else []),
        ("children", graph.children(node_id)),
        ("depends on", graph.dependencies(node_id)),
        ("needed by", graph.dependents(node_id)),
        ("waiting on", states[node_id].waiting_on),
    ):
        for other in ids:
            typer.echo(f"{label}: {names[other]} {graph[other].title}")
    if (d := node.delivery) is not None:
        parts = [d.repo, d.branch or "", d.pr or "", f"{len(d.commits)} commit(s)"]
        typer.echo("delivery: " + " · ".join(p for p in parts if p))
    if body.strip():
        typer.echo("")
        typer.echo(body.rstrip())


@node_app.command("set")
@command
def node_set(
    handle: str,
    title: Annotated[str | None, typer.Option(help="New title.")] = None,
    kind: Annotated[Kind | None, typer.Option(help="work or decision.")] = None,
) -> None:
    """Change a node's title or kind."""
    project = find_project()
    node_id = resolve(load_graph(project), handle)
    report(ops.update_node(project, node_id, title=title, kind=kind), "updated")


@node_app.command("status")
@command
def node_status(
    handle: str,
    status: Status,
    note: Annotated[str, typer.Option(help="Why: goes in the commit message.")] = "",
) -> None:
    """Set a node's status: todo, in_progress, done or dropped."""
    project = find_project()
    node_id = resolve(load_graph(project), handle)
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
        raise typer.BadParameter("give a new parent, or --top")
    project = find_project()
    graph = load_graph(project)
    target = None if parent is None else resolve(graph, parent)
    report(ops.move_node(project, resolve(graph, handle), target), "moved")


@node_app.command("rm")
@command
def node_rm(
    handle: str,
    recursive: Annotated[
        bool, typer.Option("--recursive", "-r", help="Also delete everything under it.")
    ] = False,
) -> None:
    """Delete a node and the edges that point at it."""
    project = find_project()
    node_id = resolve(load_graph(project), handle)
    result = ops.delete_node(project, node_id, recursive=recursive)
    report(result, "deleted")


@node_app.command("edit")
@command
def node_edit(handle: str) -> None:
    """Edit a node's body in $EDITOR and commit it."""
    project = find_project()
    node_id = resolve(load_graph(project), handle)
    before = read_body(project.apm_dir, node_id) or render_body()
    after = edit_text(before, suffix=f"-{handle}.md")
    report(ops.edit_body(project, node_id, after), "saved")


# --- the server, runs and proposals -------------------------------------------------------------


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
    import uvicorn

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
        webbrowser.open(url)
    uvicorn.run(
        create_app(project, token, extra_origins=origins),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


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


@app.command()
@command
def run(
    agent: Annotated[str, typer.Argument(help="decomposer, implementer or assistant.")],
    handle: str,
    guidance: Annotated[str, typer.Option("--guidance", "-g", help="What you want from it.")] = "",
    critic: Annotated[
        bool, typer.Option("--critic/--no-critic", help="Review the breakdown.")
    ] = True,
) -> None:
    """Run an agent on a node and follow it here."""
    from apm.runner.runner import Runner
    from apm.runner.transcript import format_event

    project = find_project()
    node_id = resolve(load_graph(project), handle)
    name = agent if agent.startswith("apm-") else f"apm-{agent}"
    store = Store(project.apm_dir)
    failed = []

    def show(event: dict[str, Any]) -> None:
        if event["type"] == "run.started":
            typer.echo(f"▶ {event['run']['agent']} (run {event['run']['id']})")
        elif event["type"] == "run.event":
            for line in format_event(event["event"]["data"]):
                typer.echo(line)
        elif event["type"] == "run.finished" and event["run"]["status"] != "succeeded":
            failed.append(event["run"])
            typer.echo(f"✗ {event['run']['status']}: {event['run']['error'] or ''}", err=True)
        elif event["type"] == "proposal.created":
            proposal = event["proposal"]
            count = len(proposal["data"]["children"])
            typer.echo(f"proposal {proposal['id']}: {count} children")
            typer.echo(f"next: apm proposal show {proposal['id']}")

    async def go() -> None:
        runner = Runner(project, store, show)
        try:
            runner.start(name, node_id, guidance, critic=critic)
            await runner.wait(node_id)
        finally:
            await runner.shutdown()

    try:
        asyncio.run(go())
    except RunRefused as err:
        raise OpError(str(err)) from None
    if failed:
        raise typer.Exit(1)


proposal_app = typer.Typer(
    no_args_is_help=True, help="Review what agents proposed.", context_settings=HELP
)
app.add_typer(proposal_app, name="proposal")


@proposal_app.command("ls")
@command
def proposal_ls(
    everything: Annotated[bool, typer.Option("--all", help="Include resolved ones.")] = False,
) -> None:
    """List proposals waiting for your decision."""
    project = find_project()
    names = handles(load_graph(project).nodes)
    proposals = Store(project.apm_dir).list_proposals(status=None if everything else "pending")
    for p in proposals:
        target = names.get(p.node_id, "(deleted)")
        typer.echo(f"{p.id} [{p.status}] {len(p.data.get('children', []))} children for {target}")
    if not proposals:
        typer.echo("no proposals")


@proposal_app.command("show")
@command
def proposal_show(proposal_id: str) -> None:
    """Show a proposal: the children it would create, and the review."""
    project = find_project()
    proposal = _proposal(project, proposal_id)
    data = parse_decomposition(proposal.data)
    graph = load_graph(project)
    target = graph[proposal.node_id].title if proposal.node_id in graph else "(deleted)"
    typer.echo(f"proposal {proposal.id} [{proposal.status}] for: {target}")
    if data.rationale:
        typer.echo(f"\n{data.rationale}")
    for child in data.children:
        needs = [*child.depends_on_keys, *child.depends_on_ids]
        typer.echo(
            f"\n[{child.key}] {child.title} ({child.kind})"
            + (f" → {', '.join(needs)}" if needs else "")
        )
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
    store = Store(project.apm_dir)
    children = None
    if only:
        proposal = _proposal(project, proposal_id)
        children = [c for c in proposal.data.get("children", []) if c.get("key") in only]
    result = accept_proposal(project, store, proposal_id, children)
    names = handles(result.graph.nodes)
    for node_id in result.created:
        typer.echo(f"{names[node_id]} {result.graph[node_id].title}")


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


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
