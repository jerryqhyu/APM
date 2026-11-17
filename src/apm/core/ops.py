"""The operations on a plan (plans.md §3.5). Each is one `mutate()` call, so one commit.

Node ids here are full ids: turning a handle into an id is the caller's job (`resolve_handle`).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from apm.core.body import render_body
from apm.core.config import default_repo, read_config
from apm.core.ids import handle, new_id
from apm.core.model import Delivery, Kind, Node, NodeId, Status
from apm.core.mutate import Draft, Mutation, OpError, mutate
from apm.core.ndjson import canonical_ids
from apm.core.project import Project


@dataclass(frozen=True, slots=True, kw_only=True)
class NewNode:
    title: str
    parent: NodeId | None = None
    """None = top level. `decompose()` sets it."""
    kind: Kind = Kind.WORK
    depends_on: Sequence[NodeId] = ()
    """Existing nodes."""
    key: str | None = None
    """A temporary name, so other nodes in the same batch can depend on this one before it has an
    id.
    """
    depends_on_keys: Sequence[str] = ()
    """Other nodes in the same batch, by `key`."""
    body: str | None = None
    """The whole body text. Default: the empty Intent / Acceptance criteria / Notes template."""


def create_nodes(
    project: Project, nodes: Sequence[NewNode], *, actor: str = "user", run: str | None = None
) -> Mutation:
    """Creates a batch of nodes in one commit. `Mutation.created` lists their ids in input order."""

    def apply(draft: Draft) -> str:
        created = _create(draft, nodes)
        if len(created) == 1:
            return f"{handle(created[0])}: {draft[created[0]].title}"
        return f"{len(created)} nodes"

    return mutate(project, "create", apply, actor=actor, run=run)


def decompose(
    project: Project,
    parent: NodeId,
    children: Sequence[NewNode],
    *,
    actor: str = "user",
    run: str | None = None,
    note: str = "",
) -> Mutation:
    """Adds children to `parent` in one commit. Additive only: existing children are left alone."""

    def apply(draft: Draft) -> str:
        draft[parent]
        created = _create(draft, [replace(child, parent=parent) for child in children])
        draft.touch(parent)
        return f"{handle(parent)}: {_count(len(created), 'child', 'children')}"

    return mutate(project, "decompose", apply, actor=actor, run=run, note=note)


def _create(draft: Draft, nodes: Sequence[NewNode]) -> list[NodeId]:
    if not nodes:
        raise OpError("nothing to create")
    ids = [new_id() for _ in nodes]
    by_key: dict[str, NodeId] = {}
    for node_id, new in zip(ids, nodes, strict=True):
        if new.key is not None:
            if new.key in by_key:
                raise OpError(f'duplicate key "{new.key}"')
            by_key[new.key] = node_id
    for node_id, new in zip(ids, nodes, strict=True):
        if unknown := [key for key in new.depends_on_keys if key not in by_key]:
            raise OpError(f'"{new.title}" depends on unknown key "{unknown[0]}"')
        deps = [*new.depends_on, *(by_key[key] for key in new.depends_on_keys)]
        draft.put(
            Node(
                id=node_id,
                title=new.title,
                parent=new.parent,
                kind=new.kind,
                depends_on=canonical_ids(deps),
            )
        )
        draft.set_body(node_id, render_body() if new.body is None else new.body)
    return ids


def update_node(
    project: Project,
    node_id: NodeId,
    *,
    title: str | None = None,
    kind: Kind | None = None,
    actor: str = "user",
    run: str | None = None,
) -> Mutation:
    def apply(draft: Draft) -> str:
        node = draft[node_id]
        changed = [
            name
            for name, value in (("title", title), ("kind", kind))
            if value is not None and value != getattr(node, name)
        ]
        draft.put(replace(node, title=title or node.title, kind=kind or node.kind))
        return f"{handle(node_id)}: {', '.join(changed)}"

    return mutate(project, "update", apply, actor=actor, run=run)


def edit_body(
    project: Project, node_id: NodeId, text: str, *, actor: str = "user", run: str | None = None
) -> Mutation:
    def apply(draft: Draft) -> str:
        draft.set_body(node_id, text)
        return handle(node_id)

    return mutate(project, "edit-body", apply, actor=actor, run=run)


def set_status(
    project: Project,
    node_id: NodeId,
    status: Status,
    *,
    note: str = "",
    actor: str = "user",
    run: str | None = None,
) -> Mutation:
    def apply(draft: Draft) -> str:
        draft.put(replace(draft[node_id], status=status))
        return f"{handle(node_id)}: {status}"

    return mutate(project, "status", apply, actor=actor, run=run, note=note)


def move_node(
    project: Project,
    node_id: NodeId,
    parent: NodeId | None,
    *,
    actor: str = "user",
    run: str | None = None,
) -> Mutation:
    """Moves a node, with its subtree, under `parent` (None = to the top level)."""

    def apply(draft: Draft) -> str:
        if parent is not None:
            draft[parent]
        draft.put(replace(draft[node_id], parent=parent))
        return f"{handle(node_id)} → {'top' if parent is None else handle(parent)}"

    return mutate(project, "move", apply, actor=actor, run=run)


def delete_node(
    project: Project,
    node_id: NodeId,
    *,
    recursive: bool = False,
    actor: str = "user",
    run: str | None = None,
) -> Mutation:
    """Deletes a node, and the edges that point at it, in one commit. A node with children is
    refused unless `recursive`, which deletes its whole subtree.
    """

    def apply(draft: Draft) -> str:
        title = draft[node_id].title
        descendants = draft.graph.descendants(node_id)
        if descendants and not recursive:
            count = _count(len(draft.graph.children(node_id)), "child", "children")
            raise OpError(
                f"{handle(node_id)} has {count}: delete them first, or delete recursively"
            )
        doomed = {node_id, *descendants}
        dependents = {d for n in doomed for d in draft.graph.dependents(n)} - doomed
        for dependent in dependents:
            node = draft[dependent]
            kept = tuple(d for d in node.depends_on if d not in doomed)
            draft.put(replace(node, depends_on=kept))
        for doomed_id in doomed:
            draft.remove(doomed_id)
        extra = (
            f" (+{_count(len(descendants), 'descendant', 'descendants')})" if descendants else ""
        )
        return f"{handle(node_id)}: {title}{extra}"

    return mutate(project, "delete", apply, actor=actor, run=run)


def link(
    project: Project, src: NodeId, dst: NodeId, *, actor: str = "user", run: str | None = None
) -> Mutation:
    """Records that `src` depends on `dst`."""

    def apply(draft: Draft) -> str:
        node = draft[src]
        draft.put(replace(node, depends_on=canonical_ids([*node.depends_on, draft[dst].id])))
        return f"{handle(src)} → {handle(dst)}"

    return mutate(project, "link", apply, actor=actor, run=run)


def unlink(
    project: Project, src: NodeId, dst: NodeId, *, actor: str = "user", run: str | None = None
) -> Mutation:
    """Removes the dependency of `src` on `dst`, if there is one."""

    def apply(draft: Draft) -> str:
        node = draft[src]
        draft.put(replace(node, depends_on=tuple(d for d in node.depends_on if d != dst)))
        return f"{handle(src)} from {handle(dst)}"

    return mutate(project, "unlink", apply, actor=actor, run=run)


def attach_delivery(
    project: Project,
    node_id: NodeId,
    *,
    repo: str | None = None,
    branch: str | None = None,
    commits: Iterable[str] = (),
    pr: str | None = None,
    actor: str = "user",
    run: str | None = None,
) -> Mutation:
    """Records where a leaf's work landed. Adds to what's already recorded: new commits are
    appended, and `branch` and `pr` replace the old values only when given. `repo` defaults to the
    node's current one, then to the default repo in apm.yaml.
    """
    new_commits = tuple(commits)

    def apply(draft: Draft) -> str:
        node = draft[node_id]
        config = read_config(project.apm_dir)
        old = node.delivery
        key = repo or (old.repo if old else default_repo(config))
        if key not in config.code.repos:
            raise OpError(f'unknown repo "{key}": not in apm.yaml')
        same_repo = old is not None and old.repo == key
        kept = old.commits if old and same_repo else ()
        delivery = Delivery(
            repo=key,
            branch=branch or (old.branch if old and same_repo else None),
            commits=(*kept, *(c for c in dict.fromkeys(new_commits) if c not in kept)),
            pr=pr or (old.pr if old and same_repo else None),
        )
        draft.put(replace(node, delivery=delivery))
        what = [
            f"PR {pr}" if pr else "",
            f"branch {branch}" if branch else "",
            _count(len(new_commits), "commit", "commits") if new_commits else "",
        ]
        detail = ", ".join(w for w in what if w)
        return f"{handle(node_id)}: {detail}" if detail else handle(node_id)

    return mutate(project, "deliver", apply, actor=actor, run=run)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"
