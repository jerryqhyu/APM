"""`mutate()`: the one way the plan is written (plans.md §6.2).

Each call is one semantic operation and makes exactly one commit in the plan repo, or none: a
change that would break an invariant is refused before anything is written. Any process can call
it; the write lock makes concurrent callers take turns.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apm.core.errors import ApmError
from apm.core.files import (
    GRAPH_FILE,
    NODES_DIR,
    body_path,
    read_file_if_exists,
    read_graph_file,
    write_file_atomic,
)
from apm.core.git import CommitMessage, add, commit, git, rev_parse, show
from apm.core.graph import Graph
from apm.core.ids import handle, is_node_id
from apm.core.lock import acquire_write_lock
from apm.core.model import Node, NodeId
from apm.core.ndjson import parse_graph, serialize_graph
from apm.core.project import Project
from apm.core.validate import InvariantError, validate

type Op = Literal[
    "create",
    "update",
    "move",
    "delete",
    "link",
    "unlink",
    "status",
    "deliver",
    "decompose",
    "edit-body",
    "merge",
]


class OpError(ApmError, ValueError):
    """An operation was refused for a reason other than an invariant, e.g. an unknown node."""


class DirtyPlanError(ApmError, RuntimeError):
    """Files this operation would write have uncommitted hand edits."""

    http_status = 409

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = tuple(paths)
        listed = ", ".join(self.paths)
        super().__init__(f"uncommitted changes in the plan repo: {listed}. Commit them first.")


@dataclass(frozen=True, slots=True)
class Mutation:
    sha: str | None
    """The new commit, or None if the operation changed nothing."""
    nodes: tuple[NodeId, ...]
    """Every node the operation touched, in id order."""
    created: tuple[NodeId, ...]
    """New nodes, in creation order."""
    graph: Graph
    """The graph after the operation."""


class Draft:
    """The plan as an operation is changing it. Nothing reaches the disk until the operation has
    finished and the result has passed the invariants.
    """

    def __init__(self, apm_dir: Path, nodes: Iterable[Node]) -> None:
        self._apm_dir = apm_dir
        self._nodes = {n.id: n for n in nodes}
        self._bodies: dict[NodeId, str | None] = {}  # None = delete the file
        self._touched: set[NodeId] = set()
        self._created: list[NodeId] = []
        self._graph: Graph | None = None

    @property
    def graph(self) -> Graph:
        """A read-only view of the draft as it stands."""
        if self._graph is None:
            self._graph = Graph(self._nodes.values())
        return self._graph

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def __getitem__(self, node_id: NodeId) -> Node:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise OpError(f"unknown node {node_id}") from None

    def put(self, node: Node) -> None:
        """Adds a node, or replaces the one with the same id."""
        if node.id not in self._nodes:
            self._created.append(node.id)
        elif self._nodes[node.id] == node:
            return
        self._nodes[node.id] = node
        self._touched.add(node.id)
        self._graph = None

    def remove(self, node_id: NodeId) -> None:
        """Removes a node and its body. Edges pointing at it are the caller's to clean up."""
        del self._nodes[self[node_id].id]
        self._bodies[node_id] = None
        self._touched.add(node_id)
        self._graph = None

    def touch(self, node_id: NodeId) -> None:
        """Lists a node in the commit's `APM-Nodes` although its own fields didn't change."""
        self._touched.add(self[node_id].id)

    def body(self, node_id: NodeId) -> str:
        pending = self._bodies.get(self[node_id].id)
        return pending if pending is not None else self._disk_body(node_id)

    def set_body(self, node_id: NodeId, text: str) -> None:
        if self.body(node_id) != text:
            self._bodies[node_id] = text
            self._touched.add(node_id)

    def _disk_body(self, node_id: NodeId) -> str:
        return read_file_if_exists(body_path(self._apm_dir, node_id)) or ""


def mutate(
    project: Project,
    op: Op,
    fn: Callable[[Draft], str],
    *,
    actor: str = "user",
    run: str | None = None,
    note: str = "",
    timeout: float = 10.0,
) -> Mutation:
    """Runs `fn` on a draft of the plan under the write lock, then validates, writes and commits.

    `fn` changes the draft and returns the rest of the commit subject, e.g. "1c0b9a44: done" for
    `status 1c0b9a44: done`. `note` becomes the commit body. Raises `InvariantError` if the result
    breaks a hard invariant, leaving the plan repo exactly as it was.
    """
    apm_dir = project.apm_dir
    with acquire_write_lock(apm_dir, timeout=timeout):
        before = read_graph_file(apm_dir)
        draft = Draft(apm_dir, before)
        summary = fn(draft)

        graph = draft.graph
        report = validate(graph)
        if not report.ok:
            raise InvariantError(report.errors)

        # path (relative to the plan repo) → new content, or None to delete.
        writes: dict[str, str | None] = {}
        after = sorted(graph.nodes.values(), key=lambda n: n.id)
        if after != before:
            writes[GRAPH_FILE] = serialize_graph(after)
        for node_id, text in draft._bodies.items():
            path = f"{NODES_DIR}/{node_id}.md"
            if text != read_file_if_exists(apm_dir / path):
                writes[path] = text
        touched = tuple(sorted(draft._touched))
        if not writes:
            return Mutation(None, touched, tuple(draft._created), graph)

        trailers = [("APM-Op", op), ("APM-Nodes", ",".join(touched)), ("APM-Actor", actor)]
        if run:
            trailers.append(("APM-Run", run))
        message = CommitMessage(subject=f"{op} {summary}", body=note, trailers=trailers)
        sha = _write_and_commit(apm_dir, writes, message)
        return Mutation(sha, touched, tuple(draft._created), graph)


def _write_and_commit(apm_dir: Path, writes: dict[str, str | None], message: CommitMessage) -> str:
    paths = sorted(writes)
    # Only these files are staged, so they must match HEAD first: otherwise hand edits would be
    # swept into this commit, or lost if it had to be undone.
    status = git(apm_dir, ["status", "--porcelain", "-z", "--", *paths])
    if dirty := [entry[3:] for entry in status.split("\0") if entry]:
        raise DirtyPlanError(dirty)

    previous = {path: read_file_if_exists(apm_dir / path) for path in paths}
    try:
        for path, text in writes.items():
            if text is None:
                (apm_dir / path).unlink(missing_ok=True)
            else:
                write_file_atomic(apm_dir / path, text)
        add(apm_dir, paths)
        return commit(apm_dir, message)
    except BaseException:
        # For example a commit hook refused. Put the files and the index back as they were.
        for path, text in previous.items():
            if text is None:
                (apm_dir / path).unlink(missing_ok=True)
            else:
                write_file_atomic(apm_dir / path, text)
        git(apm_dir, ["reset", "--quiet", "--", *paths])
        raise


# --- edits made by hand (plans.md §10) ---


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
