"""Committing edits made by hand in the plan repo (plans.md §10): `apm commit`.

You may edit `graph.ndjson` and the body files directly. This validates the result and commits
it as one `update` (if the graph changed) or `edit-body` (if only bodies did).
"""

from apm.core.files import (
    GRAPH_FILE,
    NODES_DIR,
    body_path,
    read_graph_file,
    write_file_atomic,
)
from apm.core.git import CommitMessage, commit, git, rev_parse, show
from apm.core.graph import Graph
from apm.core.ids import handle, is_node_id
from apm.core.lock import acquire_write_lock
from apm.core.model import Node
from apm.core.mutate import Mutation, OpError
from apm.core.ndjson import parse_graph, serialize_graph
from apm.core.project import Project
from apm.core.validate import InvariantError, validate


def commit_hand_edits(project: Project, *, actor: str = "user", timeout: float = 10.0) -> Mutation:
    """Raises `GraphParseError` or `InvariantError` if the edited plan isn't valid; nothing is
    committed then, and your edits stay as they are.
    """
    apm_dir = project.apm_dir
    with acquire_write_lock(apm_dir, timeout=timeout):
        nodes = read_graph_file(apm_dir)
        graph = Graph(nodes)
        status = git(apm_dir, ["status", "--porcelain", "-z", "-uall", "--", GRAPH_FILE, NODES_DIR])
        dirty = [entry[3:] for entry in status.split("\0") if entry]
        if not dirty:
            return Mutation(None, (), (), graph)
        report = validate(graph)
        if not report.ok:
            raise InvariantError(report.errors)

        before = {n.id: n for n in _head_graph(project)}
        after = {n.id: n for n in nodes}
        changed = {i for i in before.keys() | after.keys() if before.get(i) != after.get(i)}
        touched = set(changed)
        for removed in before.keys() - after.keys():
            body_path(apm_dir, removed).unlink(missing_ok=True)  # deleted by hand: body goes too
        for path in dirty:
            if path == GRAPH_FILE:
                continue
            node_id = path.removeprefix(f"{NODES_DIR}/").removesuffix(".md")
            if not path.endswith(".md") or not is_node_id(node_id):
                raise OpError(f"{path}: not a node body (expected {NODES_DIR}/<id>.md)")
            if node_id in after:
                touched.add(node_id)
            elif node_id not in before and (apm_dir / path).exists():
                raise OpError(f"{path}: no node has this id")

        canonical = serialize_graph(nodes)
        if (apm_dir / GRAPH_FILE).read_text(encoding="utf-8", newline="") != canonical:
            write_file_atomic(apm_dir / GRAPH_FILE, canonical)
        git(apm_dir, ["add", "--all", "--", GRAPH_FILE, NODES_DIR])
        if not git(apm_dir, ["diff", "--cached", "--name-only"]).strip():
            return Mutation(None, (), (), graph)

        op = "update" if changed else "edit-body"
        ordered = tuple(sorted(touched))
        target = handle(ordered[0]) if len(ordered) == 1 else f"{len(ordered)} nodes"
        trailers = [("APM-Op", op), ("APM-Nodes", ",".join(ordered)), ("APM-Actor", actor)]
        message = CommitMessage(subject=f"{op} {target}: hand edits", trailers=trailers)
        created = tuple(sorted(after.keys() - before.keys()))
        return Mutation(commit(apm_dir, message), ordered, created, graph)


def _head_graph(project: Project) -> list[Node]:
    if rev_parse(project.apm_dir, "HEAD") is None:
        return []
    return parse_graph(show(project.apm_dir, "HEAD", GRAPH_FILE), f"HEAD:{GRAPH_FILE}")
