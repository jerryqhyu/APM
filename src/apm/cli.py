"""The `apm` command (plans.md §10)."""

import asyncio
import contextlib
import functools
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
import yaml

from apm import __version__
from apm.core import ops
from apm.core.config import read_config
from apm.core.errors import ApmError
from apm.core.files import CONFIG_FILE, read_body, render_body
from apm.core.git import git, log
from apm.core.graph import Graph
from apm.core.ids import handle as short_handle
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
from apm.core.proposals import (
    accept_proposal,
    close_orphaned,
    parse_decomposition,
    reject_proposal,
)
from apm.core.reconcile import describe, reconcile
from apm.core.settings import (
    SETTINGS,
    check_config,
    clear_override,
    resolve_settings,
    set_defaults,
    set_override,
    settings_view,
)
from apm.core.store import Proposal, Store
from apm.core.views import (
    all_nodes_view,
    node_line,
    node_text,
    node_view,
    outline,
    pr_label,
    search,
)

if TYPE_CHECKING:  # the runner is imported only by the commands that start runs
    from apm.runner.feature_runs import Batch
    from apm.runner.runner import Runner

HELP = {"help_option_names": ["-h", "--help"]}
app = typer.Typer(name="apm", no_args_is_help=True, add_completion=False, context_settings=HELP)
node_app = typer.Typer(no_args_is_help=True, help="Work with nodes.", context_settings=HELP)
proposal_app = typer.Typer(
    no_args_is_help=True, help="Review what agents proposed.", context_settings=HELP
)
settings_app = typer.Typer(
    no_args_is_help=True, help="Show and change the settings in apm.yaml.", context_settings=HELP
)
eager_app = typer.Typer(
    no_args_is_help=True,
    help="Eager mode: a feature built in one run, then split into one pull request per task.",
    context_settings=HELP,
)
app.add_typer(node_app, name="node")
app.add_typer(proposal_app, name="proposal")
app.add_typer(settings_app, name="settings")
app.add_typer(eager_app, name="eager")

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


def cli(group: typer.Typer, name: str | None = None) -> Callable[[Callable[..., Any]], Any]:
    """Registers a command in `group`, with user errors turned into a message (see `command`)."""
    return lambda fn: group.command(name)(command(fn))


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


@cli(app)
def init(
    name: Annotated[
        str | None, typer.Option(help="Project name. Default: the folder name.")
    ] = None,
) -> None:
    """Create the plan repo (.apm/) for the code in this folder."""
    project = init_project(Path.cwd(), name=name)
    typer.echo(f"created {project.apm_dir}")
    typer.echo('next: apm serve, or apm node add "My first idea"')


@cli(app)
def check() -> None:
    """Validate the plan and apm.yaml: errors (exit code 1) and warnings."""
    project, graph = plan()
    result = check_all(graph)
    config_errors, config_warnings = check_config(project.apm_dir, graph)
    errors = [*(f"{i.code}: {i.message}" for i in result.errors), *config_errors]
    warnings = [*(f"{i.code}: {i.message}" for i in result.warnings), *config_warnings]
    for line in errors:
        typer.echo(f"error {line}")
    for line in warnings:
        typer.echo(f"warning {line}")
    if not errors and not warnings:
        typer.echo("ok")
    if errors:
        raise typer.Exit(1)


@cli(app)
def commit() -> None:
    """Validate and commit edits you made by hand in .apm/."""
    result = commit_hand_edits(find_project())
    report(result, f"committed {len(result.nodes)} node(s)")


# --- settings (apm.yaml) ------------------------------------------------------------------------


@cli(settings_app, "show")
def settings_show(
    handle: Annotated[
        str | None, typer.Argument(help="A feature or task: what applies to it, and from where.")
    ] = None,
) -> None:
    """Show the project's settings, or what applies to one node."""
    project, graph = plan()
    config = read_config(project.apm_dir)
    if handle is not None:
        node_id = graph.resolve(handle)
        resolved = resolve_settings(config, graph, node_id)
        typer.echo(f"{graph.handles[node_id]} {graph[node_id].title}")
        for name in SETTINGS:
            value, source = getattr(resolved.settings, name), resolved.sources[name]
            where = (
                "the project"
                if source is None
                else "set here"
                if source == node_id
                else f"from {graph.handles[source]} {graph[source].title}"
            )
            typer.echo(f"  {name:<16} {setting_text(value):<10} {where}")
        return
    view = settings_view(config, graph)
    for section in ("features", "automation"):
        typer.echo(f"{section}:")
        for name, value in view[section].items():
            typer.echo(f"  {name:<22} {setting_text(value)}")
    if view["overrides"]:
        typer.echo("overrides:")
    for override in view["overrides"]:
        target = override["node"]
        who = f"{graph.handles[target]} {graph[target].title}" if target else override["key"]
        values = ", ".join(f"{k} {setting_text(v)}" for k, v in override["values"].items())
        typer.echo(f"  {who}: {values or 'nothing'}")
    for problem in view["problems"]:
        typer.echo(f"warning {CONFIG_FILE}: {problem}")


@cli(settings_app, "set")
def settings_set(
    pairs: Annotated[
        list[str],
        typer.Argument(
            help="NAME=VALUE: eager=on, task_max_lines=80, report_detail=brief,"
            " automation.task_review=off. VALUE default (or inherit) unsets it.",
        ),
    ],
    node: Annotated[
        str | None,
        typer.Option("--node", "-n", help="Change this feature's own settings instead."),
    ] = None,
) -> None:
    """Change settings in apm.yaml, keeping its comments: one commit in the plan repo."""
    project, graph = plan()
    parsed = [parse_setting(pair) for pair in pairs]
    by_section: dict[str, dict[str, Any]] = {"features": {}, "automation": {}}
    for section, name, value in parsed:
        by_section[section][name] = value
    if node is not None:
        if by_section["automation"]:
            raise OpError("automation switches are project-wide: leave out --node")
        change = set_override(project, graph.resolve(node), by_section["features"])
    else:
        features, automation = by_section["features"], by_section["automation"]
        change = set_defaults(project, features=features, automation=automation)
    typer.echo(log(project.apm_dir)[0].subject if change.sha else "nothing to change")


@cli(settings_app, "clear")
def settings_clear(handle: str) -> None:
    """Remove a feature's own settings, so it inherits them all again."""
    project, graph = plan()
    change = clear_override(project, graph.resolve(handle))
    typer.echo(log(project.apm_dir)[0].subject if change.sha else "nothing to clear")


def parse_setting(pair: str) -> tuple[str, str, bool | int | float | str | None]:
    """`automation.task_review=off` as ("automation", "task_review", False). A name without a
    section is a feature setting.
    """
    name, equals, raw = pair.partition("=")
    section, _, key = name.strip().rpartition(".")
    section = section or "features"
    if not equals or not key or section not in ("features", "automation"):
        raise OpError(f'"{pair}": expected NAME=VALUE, e.g. eager=on or automation.reconcile=off')
    if raw.strip() in ("default", "inherit"):
        return section, key, None
    try:
        value = yaml.safe_load(raw)  # on, off, yes, no, true, false, 80, brief
    except yaml.YAMLError:
        value = raw
    if value is None or not isinstance(value, bool | int | float | str):
        raise OpError(f'"{pair}": {key} needs a value')
    return section, key, value


def setting_text(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


@cli(app, "reconcile")
def reconcile_(
    retarget: Annotated[
        bool,
        typer.Option(
            "--retarget", help="Point open PRs whose base has landed at the default branch."
        ),
    ] = False,
) -> None:
    """Mark in-progress nodes done once their work is on the default branch."""
    lines = describe(reconcile(find_project(), retarget=retarget))
    for line in lines:
        typer.echo(line)
    if not lines:
        typer.echo("nothing to reconcile")


@cli(app)
def ready(
    root_: Annotated[str | None, typer.Option("--root", help="Only under this node.")] = None,
    as_json: Json = False,
) -> None:
    """List what can be worked on now: ready leaves, stacked ones marked."""
    _, graph = plan()
    ids = graph.ready_leaves(graph.resolve(root_) if root_ else None)
    if as_json:
        print_json([node_view(graph, n) for n in ids])
        return
    for node_id in ids:
        typer.echo(node_line(graph, node_id))


@cli(app, "search")
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


@cli(app, "link-dep")
def link_dep(src: str, dst: str) -> None:
    """Record that SRC depends on DST."""
    project, graph = plan()
    report(ops.link(project, graph.resolve(src), graph.resolve(dst)), "linked")


@cli(app, "unlink-dep")
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


@cli(app)
def link(remote: Annotated[str, typer.Option(help="URL of the plan repo's remote.")]) -> None:
    """Set the plan repo's remote and record it in .apm-link."""
    if not remote.strip():
        raise OpError("the remote can't be empty")
    project = find_project()
    remotes = git(project.apm_dir, ["remote"]).split()
    action = ["set-url"] if "origin" in remotes else ["add"]
    git(project.apm_dir, ["remote", *action, "origin", remote])
    link_file = project.code_dir / LINK_FILE
    data = json.loads(link_file.read_text(encoding="utf-8"))
    link_file.write_text(json.dumps({**data, "remote": remote}) + "\n", encoding="utf-8")
    typer.echo(f"plan repo remote: {remote}")


@cli(app)
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


@cli(app)
def push() -> None:
    """Push the plan repo."""
    typer.echo(git(find_project().apm_dir, ["push", "--quiet", "-u", "origin", "HEAD"]) or "pushed")


@cli(app)
def pull() -> None:
    """Pull the plan repo (fast-forward only)."""
    typer.echo(git(find_project().apm_dir, ["pull", "--quiet", "--ff-only"]) or "up to date")


# --- archives -----------------------------------------------------------------------------------

RUNS_WARNING = (
    "warning: {file} holds run transcripts, which can include tool output and secrets"
    " (file contents, tokens, keys). Check it before you share it."
)


@cli(app, "export")
def export(
    file: Annotated[Path, typer.Argument(help="Where to write the archive, e.g. plan.apm.zip.")],
    with_runs: Annotated[
        bool,
        typer.Option("--with-runs", help="Also include runs, their transcripts, and proposals."),
    ] = False,
) -> None:
    """Save the project and its history as one .apm.zip file."""
    from apm.core.archive import export_archive

    manifest = export_archive(find_project(), file, with_runs=with_runs)
    typer.echo(f"exported {manifest['project']} to {file}: {_counts(manifest['counts'])}")
    if with_runs:
        typer.echo(RUNS_WARNING.format(file=file), err=True)


@cli(app, "import")
def import_(
    file: Annotated[Path, typer.Argument(help="The .apm.zip archive to restore.")],
) -> None:
    """Restore a project from an .apm.zip file into this folder."""
    from apm.core.archive import import_archive

    imported = import_archive(file, Path.cwd())
    manifest = imported.manifest
    typer.echo(
        f"imported {manifest['project']} into {imported.project.apm_dir}:"
        f" {_counts(manifest['counts'])}"
    )
    typer.echo("next: apm check, or apm serve")


def _counts(counts: dict[str, Any]) -> str:
    return ", ".join(f"{value} {key}" for key, value in counts.items())


# --- nodes --------------------------------------------------------------------------------------


@cli(node_app, "add")
def node_add(
    title: str,
    parent: Annotated[str | None, typer.Option("--parent", "-p", help="Parent node.")] = None,
    kind: Annotated[Kind, typer.Option(help="work or decision.")] = Kind.WORK,
    dep: Annotated[list[str] | None, typer.Option("--dep", help="A node this depends on.")] = None,
    intent: Annotated[str, typer.Option(help="Why it exists: goes in the body.")] = "",
    tag: Annotated[
        list[str] | None, typer.Option("--tag", "-t", help="A tag (repeatable).")
    ] = None,
) -> None:
    """Add a node."""
    project, graph = plan()
    new = ops.NewNode(
        title=title,
        parent=graph.resolve(parent) if parent else None,
        kind=kind,
        depends_on=[graph.resolve(d) for d in dep or []],
        body=render_body(intent=intent),
        tags=tag or [],
    )
    result = ops.create_nodes(project, [new])
    typer.echo(f"{result.graph.handles[result.created[0]]} {title}")


@cli(node_app, "ls")
def node_ls(
    root_: Annotated[str | None, typer.Option("--root", help="Only under this node.")] = None,
    depth: Annotated[int | None, typer.Option(help="How many levels to show.")] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", "-t", help="Only nodes with this tag (repeatable).")
    ] = None,
    as_json: Json = False,
) -> None:
    """Show the plan as an outline, or, with --tag, the nodes that have every tag given."""
    _, graph = plan()
    scope = graph.resolve(root_) if root_ else None
    inside = None if scope is None else set(graph.descendants(scope))
    if tag:
        wanted = set(ops.clean_tags(tag))
        views = [n for n in all_nodes_view(graph) if wanted <= set(n["tags"])]
        views = [n for n in views if inside is None or n["id"] in inside]
        if as_json:
            print_json(views)
        for view in [] if as_json else views:
            typer.echo(node_line(graph, view["id"]))
        if not views and not as_json:
            typer.echo("no nodes with " + ", ".join(f"#{t}" for t in sorted(wanted)))
        return
    if as_json:
        print_json([n for n in all_nodes_view(graph) if inside is None or n["id"] in inside])
        return
    typer.echo(outline(graph, scope, depth) or '(no nodes yet: apm node add "...")')


@cli(node_app, "tag")
def node_tag(
    handle: str,
    tags: Annotated[list[str], typer.Argument(help="Tags to add, e.g. backlog area/ui.")],
    remove: Annotated[bool, typer.Option("--remove", "-r", help="Remove them instead.")] = False,
) -> None:
    """Add tags to a node, or remove them."""
    project, _, node_id = locate(handle)
    added, removed = ([], tags) if remove else (tags, [])
    result = ops.set_tags(project, node_id, add=added, remove=removed)
    report(result, " ".join(f"#{t}" for t in result.graph[node_id].tags) or "no tags")


@cli(node_app, "show")
def node_show(handle: str, as_json: Json = False) -> None:
    """Show one node: its state, links and body."""
    project, graph, node_id = locate(handle)
    if as_json:
        print_json({**node_view(graph, node_id), "body": read_body(project.apm_dir, node_id)})
        return
    typer.echo(node_text(project, graph, node_id))


@cli(node_app, "set")
def node_set(
    handle: str,
    title: Annotated[str | None, typer.Option(help="New title.")] = None,
    kind: Annotated[Kind | None, typer.Option(help="work or decision.")] = None,
) -> None:
    """Change a node's title or kind."""
    project, _, node_id = locate(handle)
    report(ops.update_node(project, node_id, title=title, kind=kind), "updated")


@cli(node_app, "status")
def node_status(
    handle: str,
    status: Annotated[Status, typer.Argument(case_sensitive=False)],
    note: Annotated[str, typer.Option(help="Why: goes in the commit message.")] = "",
) -> None:
    """Set a node's status: todo, in_progress, done or dropped."""
    project, _, node_id = locate(handle)
    report(ops.set_status(project, node_id, status, note=note), f"{handle}: {status}")


@cli(node_app, "mv")
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


@cli(node_app, "merge")
def node_merge(
    target: Annotated[str, typer.Argument(help="The node that survives.")],
    others: Annotated[list[str], typer.Argument(help="Nodes folded into it, then deleted.")],
    title: Annotated[str | None, typer.Option(help="New title for the merged node.")] = None,
) -> None:
    """Merge several nodes into one: children, dependencies and bodies move to the target."""
    project, graph, node_id = locate(target)
    result = ops.merge_nodes(project, node_id, [graph.resolve(o) for o in others], title=title)
    report(result, f"merged {len(others) + 1} nodes into {target}")


@cli(node_app, "rm")
def node_rm(
    handle: str,
    recursive: Annotated[
        bool, typer.Option("--recursive", "-r", help="Also delete everything under it.")
    ] = False,
) -> None:
    """Delete a node and the edges that point at it."""
    project, _, node_id = locate(handle)
    report(ops.delete_node(project, node_id, recursive=recursive), "deleted")


@cli(node_app, "edit")
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


@cli(app)
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


@cli(app)
def run(
    agent: Annotated[
        str,
        typer.Argument(
            help="assistant, decomposer, implementer, feature-reviewer or task-reviewer."
        ),
    ],
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
    branch: Annotated[
        str | None,
        typer.Option(help="feature-reviewer: the branch to review. Default: APM works it out."),
    ] = None,
) -> None:
    """Run an agent and follow it here. It asks you in the terminal when it needs an answer."""
    project, graph = plan()
    node_id = graph.resolve(handle) if handle else None
    name = agent if agent.startswith("apm-") else f"apm-{agent}"

    async def work(runner: Runner) -> None:
        started = runner.start(name, node_id, message, critic=critic, mode=mode, branch=branch)
        await runner.wait(started.id)

    if follow_runs(project, work):
        raise typer.Exit(1)


Mode = Annotated[
    str | None, typer.Option(help="Permission mode: auto, manual, acceptEdits, plan or dontAsk.")
]


@cli(app, "review-tasks")
def review_tasks(
    handle: Annotated[str, typer.Argument(help="The feature (or one task).")],
    mode: Mode = None,
) -> None:
    """Review every To do task of a feature, nested features included, and follow the reviews
    here. Up to runner.max_concurrent run at once; the rest start as those finish.
    """
    from apm.runner.feature_runs import review_all_tasks

    run_batch(handle, review_all_tasks, "nothing to review: it has no To do tasks", mode=mode)


@cli(app, "implement")
def implement(
    handle: Annotated[str, typer.Argument(help="The feature (or one task).")],
    message: Annotated[
        str, typer.Option("--message", "-m", "--guidance", "-g", help="Guidance for every task.")
    ] = "",
    mode: Mode = None,
) -> None:
    """Build every task of a feature that is ready now and has passed its task review, and follow
    the runs here. Up to runner.max_concurrent run at once; the rest start as those finish.
    """
    from apm.runner.feature_runs import implement_feature

    empty = "nothing to implement: it has no To do tasks"
    run_batch(handle, implement_feature, empty, mode=mode, message=message)


@cli(app, "report")
def report_(
    handle: Annotated[str, typer.Argument(help="The feature.")],
    message: Annotated[
        str, typer.Option("--message", "-m", "--guidance", "-g", help="Guidance for the report.")
    ] = "",
    mode: Mode = None,
    show: Annotated[
        bool, typer.Option("--show", help="Print the latest report instead of writing one.")
    ] = False,
) -> None:
    """Write the feature's report and commit it to its branch as docs/features/<name>.md, or show
    the latest one. Its pull request, if open, links it.
    """
    from apm.core.report import REPORTER
    from apm.core.report_branch import report_status

    project, _, node_id = locate(handle)
    if show:
        status = report_status(project, Store(project.apm_dir), node_id)
        report = status.report
        if report is None:
            typer.echo("no report yet" + (": one is being written" if status.running else ""))
            return
        where = f"commit {report.sha[:7]}" if report.sha else f"not committed: {report.error}"
        typer.echo(f"{report.path} on {report.branch}, {where} ({report.detail})")
        if status.url:
            typer.echo(status.url)
        typer.echo(f"\n{report.markdown.rstrip()}")
        return

    async def work(runner: Runner) -> None:
        started = runner.start(REPORTER, node_id, message, mode=mode)
        await runner.wait(started.id)

    if follow_runs(project, work):
        raise typer.Exit(1)


@cli(eager_app, "show")
def eager_show(handle: Annotated[str, typer.Argument(help="The feature.")]) -> None:
    """Show a feature's eager build: its branch, each task's part and pull request, and what a
    Redo would do.
    """
    from apm.core.eager import eager_view

    project, graph, node_id = locate(handle)
    store, config = Store(project.apm_dir), read_config(project.apm_dir)
    view = eager_view(store, config, graph, node_id)
    for line in describe_eager(graph, node_id, view):
        typer.echo(line)


def describe_eager(graph: Graph, node_id: NodeId, view: dict[str, Any] | None) -> list[str]:
    """A feature's eager build as `apm eager show` prints it."""
    named = f"{graph.handles[node_id]} {graph[node_id].title}"
    if view is None or "build" not in view:
        return [f"{named}: eager mode is off, and nothing was built eagerly"]
    build = view["build"]
    if build is None:
        return [
            f"{named}: eager mode is on; nothing built yet (apm implement {graph.handles[node_id]})"
        ]
    lines = [
        f"{named}: eager build {build['round']} on {build['branch']}, {build['state']}"
        f" (from {build['base']} at {build['base_sha'][:7]})"
    ]
    if build["error"]:
        lines.append(f"  error: {build['error']}")
    for task in view["tasks"]:
        pr = pr_label(task["pr"]) or "no PR"
        state = "stale: will be rebuilt" if task["state"] == "stale" else task["state"]
        lines.append(
            f"  {task['position'] + 1}. {task['handle']} {task['title']}  {task['branch']}"
            f"  {pr}  {state}"
        )
    pending = view["pending"]
    if pending and pending.get("node"):
        what = "new commits" if pending["kind"] == "pushed" else "merged with changes"
        lines.append(f"changed: {pending['node']['handle']} {pending['node']['title']} ({what})")
    if pending and pending.get("blocked"):
        lines.append(f"can't redo on its own: {pending['blocked']}")
    lines += [f"warning: {w}" for w in build["warnings"]]
    if view["redo"]["action"]:
        lines.append(f"redo: {view['redo']['why']} (apm eager redo {graph.handles[node_id]})")
    return lines


@cli(eager_app, "redo")
def eager_redo(
    handle: Annotated[str, typer.Argument(help="The feature.")],
    message: Annotated[
        str, typer.Option("--message", "-m", "--guidance", "-g", help="Guidance for the rebuild.")
    ] = "",
    mode: Mode = None,
) -> None:
    """Rebuild the tasks after one whose pull request changed since the split (or split a build
    again whose split failed), and follow it here.
    """
    project, _, node_id = locate(handle)

    async def work(runner: Runner) -> None:
        started = await runner.eager.redo(node_id, message, mode=mode)
        await runner.wait(started.id)

    if follow_runs(project, work):
        raise typer.Exit(1)


POLL_S = 0.2
"""How often `run_batch` looks for room to start a waiting run."""


def run_batch(
    handle: str,
    start: Callable[..., Batch],
    empty: str,
    *,
    mode: str | None,
    message: str = "",
) -> None:
    """Starts a feature-level action in this process, says what it started, queued and skipped,
    then starts the queued runs as others finish, following them all until the last one ends.
    """
    from apm.runner.queue import RunQueue

    project, graph, node_id = locate(handle)

    async def work(runner: Runner) -> None:
        queue = RunQueue(runner)
        batch = start(queue, node_id, mode=mode, message=message)
        for line in describe_batch(graph, batch) or [empty]:
            typer.echo(line)
        while queue.waiting or runner.active():
            await asyncio.sleep(POLL_S)
            for dropped in queue.pump().dropped:
                # By handle alone: the node may be gone, which is often why it was dropped.
                typer.echo(f"skipped {short_handle(dropped.item.node_id)}: {dropped.reason}")

    if follow_runs(project, work, tag=True):
        raise typer.Exit(1)


def describe_batch(graph: Graph, batch: Batch) -> list[str]:
    """What a feature-level action started, queued and skipped, one line each."""

    def named(node_id: NodeId) -> str:
        return f"{graph.handles[node_id]} {graph[node_id].title}"

    lines = []
    if batch.started:
        lines.append(
            f"started {len(batch.started)}: {', '.join(named(r.node_id) for r in batch.started)}"
        )
    if batch.waiting:
        waiting = ", ".join(named(n) for n in batch.waiting)
        lines.append(
            f"waiting for room under runner.max_concurrent, {len(batch.waiting)}: {waiting}"
        )
    lines += [f"skipped {named(s.node_id)}: {s.reason}" for s in batch.skipped]
    return lines


def follow_runs(
    project: Project, work: Callable[[Runner], Awaitable[None]], *, tag: bool = False
) -> bool:
    """Runs `work` with a runner whose runs print here, and whose questions and permission
    prompts are put to you here. With `tag`, each line starts with the handle of the node its run
    is about, for several runs at once. True if a run failed.
    """
    from apm.runner.agents import RunRefused
    from apm.runner.runner import Runner
    from apm.runner.transcript import format_event

    store = Store(project.apm_dir)
    failed: list[dict[str, Any]] = []
    waiting: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    nodes: dict[str, str] = {}  # run id → its node's handle

    def echo(run_id: str, text: str, *, err: bool = False) -> None:
        typer.echo(f"[{nodes[run_id]}] {text}" if tag and run_id in nodes else text, err=err)

    def show(event: dict[str, Any]) -> None:
        if event["type"] == "run.started":
            run = event["run"]
            nodes[run["id"]] = short_handle(run["node_id"])
            echo(run["id"], f"▶ {run['agent']} (run {run['id']})")
        elif event["type"] == "run.event":
            for line in format_event(event["event"]["data"]):
                echo(event["run_id"], line)
        elif event["type"] == "run.request":
            waiting.put_nowait(event["request"])
        elif event["type"] == "run.finished" and event["run"]["status"] != "succeeded":
            run = event["run"]
            failed.append(run)
            echo(run["id"], f"✗ {run['status']}: {run['error'] or ''}", err=True)
        elif event["type"] == "proposal.created":
            proposal = event["proposal"]
            count = len(proposal["data"]["children"])
            typer.echo(
                f"proposal {proposal['id']}: {count} {'child' if count == 1 else 'children'}"
            )
            typer.echo(f"next: apm proposal show {proposal['id']}")

    async def go() -> None:
        runner = Runner(project, store, show)

        async def answer_requests() -> None:
            while True:
                request = await waiting.get()
                try:
                    allow, answers = await asyncio.to_thread(ask_in_terminal, request)
                    message = ""
                except Exception, KeyboardInterrupt:
                    # Nobody is at the terminal, or input ran out (typer.Abort, EOFError): say no.
                    # The run would otherwise wait for an answer that never comes.
                    allow, answers, message = False, None, "Nobody answered at the terminal."
                    typer.echo("  (no answer: refused)")
                with contextlib.suppress(RunRefused):  # the run ended meanwhile
                    await runner.answer(
                        request["id"], allow=allow, answers=answers, message=message
                    )

        asker = asyncio.create_task(answer_requests())
        try:
            await work(runner)
        finally:
            asker.cancel()
            await runner.shutdown()

    asyncio.run(go())
    return bool(failed)


def ask_in_terminal(request: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
    """Puts an agent's question or permission prompt to the person at the terminal."""
    tool_input = request["input"]
    if request["kind"] == "plan":
        typer.echo("? Claude has a plan and waits for your go-ahead:\n")
        typer.echo(textwrap.indent(str(tool_input.get("plan", "")).strip(), "    "))
        return typer.confirm("\n  go ahead?", default=False), None
    if request["kind"] != "question":
        what = (
            tool_input.get("command")
            or tool_input.get("file_path")
            or tool_input.get("url")
            or json.dumps(tool_input, ensure_ascii=False)[:300]
        )
        why = f"  ({request['description']})" if request["description"] else ""
        typer.echo(f"? {request['tool']}: {what}{why}")
        return typer.confirm("  allow?", default=False), None
    answers = {}
    for question in request["input"].get("questions", []):
        options = question.get("options", [])
        typer.echo(f"? {question['question']}")
        for number, option in enumerate(options, start=1):
            typer.echo(f"  {number}. {option['label']}")
        many = bool(question.get("multiSelect"))
        hint = "numbers separated by commas" if many else "a number"
        choice = typer.prompt(f"  your choice ({hint}, or your own words)", default="1")
        numbers = [part.strip() for part in choice.split(",")] if many else [choice.strip()]
        picked = [int(n) - 1 for n in numbers if n.isdigit() and 0 < int(n) <= len(options)]
        if picked and len(picked) == len(numbers):
            answers[question["question"]] = ", ".join(options[i]["label"] for i in picked)
        else:
            answers[question["question"]] = choice
    return True, answers


# --- proposals ----------------------------------------------------------------------------------


@cli(proposal_app, "ls")
def proposal_ls(
    everything: Annotated[bool, typer.Option("--all", help="Include resolved ones.")] = False,
) -> None:
    """List proposals waiting for your decision."""
    project, graph = plan()
    store = Store(project.apm_dir)
    close_orphaned(store, graph)
    proposals = store.list_proposals(status=None if everything else "pending")
    for p in proposals:
        target = graph.handles.get(p.node_id, "(deleted)")
        typer.echo(f"{p.id} [{p.status}] {len(p.data.get('children', []))} children for {target}")
    if not proposals:
        typer.echo("no proposals")


@cli(proposal_app, "show")
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


@cli(proposal_app, "accept")
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


@cli(proposal_app, "reject")
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
