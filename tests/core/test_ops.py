import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apm.core.body import read_body, render_body
from apm.core.git import git, log, rev_parse
from apm.core.ids import handle
from apm.core.model import Delivery, Kind, Status
from apm.core.mutate import Mutation, OpError
from apm.core.ops import (
    NewNode,
    attach_delivery,
    create_nodes,
    decompose,
    delete_node,
    edit_body,
    link,
    move_node,
    set_status,
    unlink,
    update_node,
)
from apm.core.project import Project, init_project, load_graph
from apm.core.validate import InvariantError, validate

SHA_1, SHA_2 = "a1b2c3d", "e4f5a6b7"


def subject(project: Project) -> str:
    return log(project.apm_dir)[0].subject


def add(project: Project, title: str, parent: str | None = None, deps: tuple[str, ...] = ()) -> str:
    result = create_nodes(project, [NewNode(title=title, parent=parent, depends_on=deps)])
    return result.created[0]


def test_create_one_node_with_the_body_template(project: Project) -> None:
    result = create_nodes(project, [NewNode(title="Stream output", kind=Kind.DECISION)])
    (node_id,) = result.created
    node = load_graph(project)[node_id]
    assert (node.title, node.kind, node.status, node.parent) == (
        "Stream output",
        Kind.DECISION,
        Status.TODO,
        None,
    )
    assert read_body(project.apm_dir, node_id) == render_body()
    assert subject(project) == f"create {handle(node_id)}: Stream output"
    assert log(project.apm_dir)[0].trailers["APM-Nodes"] == node_id


def test_create_a_batch_wired_by_keys_in_one_commit(project: Project) -> None:
    existing = add(project, "Existing")
    result = create_nodes(
        project,
        [
            NewNode(title="Parse", key="parse"),
            NewNode(title="Write", key="write", depends_on_keys=["parse"], depends_on=[existing]),
            NewNode(title="Docs", body="## Intent\n\nExplain it.\n"),
        ],
    )
    parse, write, docs = result.created
    assert parse < write < docs
    graph = load_graph(project)
    assert graph[write].depends_on == (existing, parse)
    assert read_body(project.apm_dir, docs) == "## Intent\n\nExplain it.\n"
    assert subject(project) == "create 3 nodes"
    assert len(log(project.apm_dir)) == 3  # init, Existing, the batch


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([], "nothing to create"),
        ([NewNode(title="a", key="k"), NewNode(title="b", key="k")], 'duplicate key "k"'),
        ([NewNode(title="a", depends_on_keys=["nope"])], 'unknown key "nope"'),
    ],
)
def test_create_rejects_a_bad_batch(project: Project, nodes: list[NewNode], message: str) -> None:
    with pytest.raises(OpError, match=message):
        create_nodes(project, nodes)
    assert len(log(project.apm_dir)) == 1


def test_create_refuses_a_missing_parent_or_a_blank_title(project: Project) -> None:
    missing = "01930000-0000-7000-8000-000000000001"
    with pytest.raises(InvariantError, match="I1"):
        create_nodes(project, [NewNode(title="orphan", parent=missing)])
    with pytest.raises(ValueError, match="must not be blank"):
        create_nodes(project, [NewNode(title="  ")])
    assert len(load_graph(project)) == 0
    assert git(project.apm_dir, ["status", "--porcelain"]) == ""


def test_decompose_adds_children_and_names_the_parent(project: Project) -> None:
    parent = add(project, "Big idea")
    result = decompose(
        project,
        parent,
        [NewNode(title="First", key="a"), NewNode(title="Second", depends_on_keys=["a"])],
        actor="agent:apm-decomposer",
        note="Two steps.",
    )
    first, second = result.created
    graph = load_graph(project)
    assert graph.children(parent) == (first, second)
    assert graph[second].depends_on == (first,)
    assert result.nodes == (parent, first, second)
    entry = log(project.apm_dir)[0]
    assert entry.subject == f"decompose {handle(parent)}: 2 children"
    assert entry.trailers["APM-Nodes"] == f"{parent},{first},{second}"
    assert entry.trailers["APM-Actor"] == "agent:apm-decomposer"

    # Additive: a second round keeps the first.
    decompose(project, parent, [NewNode(title="Third")])
    assert len(load_graph(project).children(parent)) == 3
    assert subject(project) == f"decompose {handle(parent)}: 1 child"


def test_decompose_refuses_a_node_that_carries_a_delivery(project: Project) -> None:
    leaf = add(project, "Leaf")
    attach_delivery(project, leaf, branch="apm/leaf")
    with pytest.raises(InvariantError, match="I7"):
        decompose(project, leaf, [NewNode(title="Child")])


def test_update_changes_the_title_and_kind(project: Project) -> None:
    node_id = add(project, "Old")
    update_node(project, node_id, title="New")
    assert subject(project) == f"update {handle(node_id)}: title"
    update_node(project, node_id, title="Newer", kind=Kind.DECISION)
    assert subject(project) == f"update {handle(node_id)}: title, kind"
    node = load_graph(project)[node_id]
    assert (node.title, node.kind) == ("Newer", Kind.DECISION)
    assert update_node(project, node_id, title="Newer").sha is None


def test_edit_body_commits_only_the_body(project: Project) -> None:
    node_id = add(project, "Node")
    result = edit_body(project, node_id, "## Intent\n\nChanged.\n")
    assert read_body(project.apm_dir, node_id) == "## Intent\n\nChanged.\n"
    assert subject(project) == f"edit-body {handle(node_id)}"
    changed = git(project.apm_dir, ["show", "--name-only", "--format=", "HEAD"]).split()
    assert changed == [f"nodes/{node_id}.md"]
    assert result.nodes == (node_id,)
    assert edit_body(project, node_id, "## Intent\n\nChanged.\n").sha is None


def test_set_status_records_the_note(project: Project) -> None:
    node_id = add(project, "Node")
    set_status(project, node_id, Status.DROPPED, note="Superseded.")
    assert load_graph(project)[node_id].status is Status.DROPPED
    assert subject(project) == f"status {handle(node_id)}: dropped"
    assert git(project.apm_dir, ["log", "-1", "--format=%b"]).startswith("Superseded.\n")
    assert set_status(project, node_id, Status.DROPPED).sha is None


def test_move_reparents_a_subtree(project: Project) -> None:
    a = add(project, "A")
    b = add(project, "B")
    child = add(project, "Child", parent=a)
    grandchild = add(project, "Grandchild", parent=child)
    move_node(project, child, b)
    graph = load_graph(project)
    assert graph.ancestors(grandchild) == [child, b]
    assert subject(project) == f"move {handle(child)} → {handle(b)}"
    move_node(project, child, None)
    assert load_graph(project).children(None) == (a, b, child)
    assert subject(project) == f"move {handle(child)} → top"


def test_move_refuses_a_cycle_or_a_broken_dependency(project: Project) -> None:
    a = add(project, "A")
    child = add(project, "Child", parent=a)
    other = add(project, "Other", deps=(a,))
    with pytest.raises(InvariantError, match="I1: containment cycle"):
        move_node(project, a, child)
    with pytest.raises(InvariantError, match="I3"):
        move_node(project, other, a)
    with pytest.raises(OpError, match="unknown node"):
        move_node(project, child, "01930000-0000-7000-8000-000000000001")


def test_delete_removes_the_node_its_body_and_edges_to_it(project: Project) -> None:
    target = add(project, "Target")
    dependent = add(project, "Dependent", deps=(target,))
    result = delete_node(project, target)
    graph = load_graph(project)
    assert target not in graph
    assert graph[dependent].depends_on == ()
    assert not (project.apm_dir / "nodes" / f"{target}.md").exists()
    assert result.nodes == (target, dependent)
    assert subject(project) == f"delete {handle(target)}: Target"


def test_delete_needs_recursive_for_a_node_with_children(project: Project) -> None:
    parent = add(project, "Parent")
    child = add(project, "Child", parent=parent)
    grandchild = add(project, "Grandchild", parent=child)
    outside = add(project, "Outside", deps=(grandchild,))
    with pytest.raises(OpError, match="has 1 child: delete them first, or delete recursively"):
        delete_node(project, parent)
    assert len(load_graph(project)) == 4

    commits = len(log(project.apm_dir))
    delete_node(project, parent, recursive=True)
    graph = load_graph(project)
    assert set(graph.nodes) == {outside}
    assert graph[outside].depends_on == ()
    assert len(log(project.apm_dir)) == commits + 1
    assert subject(project) == f"delete {handle(parent)}: Parent (+2 descendants)"
    assert list((project.apm_dir / "nodes").iterdir()) == [
        project.apm_dir / "nodes" / f"{outside}.md"
    ]


def test_link_and_unlink(project: Project) -> None:
    a = add(project, "A")
    b = add(project, "B")
    link(project, a, b)
    assert load_graph(project)[a].depends_on == (b,)
    assert subject(project) == f"link {handle(a)} → {handle(b)}"
    assert link(project, a, b).sha is None

    with pytest.raises(InvariantError, match="I2: dependency cycle"):
        link(project, b, a)

    unlink(project, a, b)
    assert load_graph(project)[a].depends_on == ()
    assert subject(project) == f"unlink {handle(a)} from {handle(b)}"
    assert unlink(project, a, b).sha is None


def test_attach_delivery_builds_up_over_calls(project: Project) -> None:
    leaf = add(project, "Leaf")
    attach_delivery(project, leaf, branch="apm/leaf", commits=[SHA_1])
    assert subject(project) == f"deliver {handle(leaf)}: branch apm/leaf, 1 commit"
    attach_delivery(project, leaf, commits=[SHA_1, SHA_2], pr="https://example.com/pull/12")
    assert load_graph(project)[leaf].delivery == Delivery(
        repo="my-app",
        branch="apm/leaf",
        commits=(SHA_1, SHA_2),
        pr="https://example.com/pull/12",
    )
    assert subject(project) == f"deliver {handle(leaf)}: PR https://example.com/pull/12, 2 commits"


def test_attach_delivery_checks_the_repo_the_leaf_and_the_values(project: Project) -> None:
    parent = add(project, "Parent")
    add(project, "Child", parent=parent)
    with pytest.raises(OpError, match='unknown repo "nope"'):
        attach_delivery(project, parent, repo="nope")
    with pytest.raises(InvariantError, match="I7"):
        attach_delivery(project, parent, branch="x")
    leaf = add(project, "Leaf")
    with pytest.raises(ValueError, match="must be a hex commit SHA"):
        attach_delivery(project, leaf, commits=["not a sha"])
    assert load_graph(project)[leaf].delivery is None


# --- Random operation sequences ---------------------------------------------------------------

STATUSES = st.sampled_from(Status)
PICK = st.integers(0, 2**16)
OPS = st.one_of(
    st.tuples(st.just("create"), PICK, st.booleans()),
    st.tuples(st.just("status"), PICK, STATUSES),
    st.tuples(st.just("move"), PICK, PICK, st.booleans()),
    st.tuples(st.just("link"), PICK, PICK),
    st.tuples(st.just("unlink"), PICK, PICK),
    st.tuples(st.just("delete"), PICK, st.booleans()),
    st.tuples(st.just("deliver"), PICK),
    st.tuples(st.just("decompose"), PICK),
)


# Every sequence starts from a small tree, so the random ops have something to act on.
SEED = [("create", 0, True), ("create", 0, False), ("create", 1, False), ("create", 0, True)]


def run_op(project: Project, op: tuple) -> Mutation | None:
    """Runs one random op against whatever nodes exist. Returns None if there's nothing to run it
    on.
    """
    ids = sorted(load_graph(project).nodes)

    def pick(n: int) -> str:
        return ids[n % len(ids)]

    match op:
        case ("create", n, top):
            parent = None if top or not ids else pick(n)
            return create_nodes(project, [NewNode(title="new", parent=parent)])
        case _ if not ids:
            return None
        case ("status", n, status):
            return set_status(project, pick(n), status)
        case ("move", n, m, top):
            return move_node(project, pick(n), None if top else pick(m))
        case ("link", n, m):
            return link(project, pick(n), pick(m))
        case ("unlink", n, m):
            return unlink(project, pick(n), pick(m))
        case ("delete", n, recursive):
            return delete_node(project, pick(n), recursive=recursive)
        case ("deliver", n):
            return attach_delivery(project, pick(n), branch="apm/x")
        case ("decompose", n):
            return decompose(
                project,
                pick(n),
                [NewNode(title="one", key="a"), NewNode(title="two", depends_on_keys=["a"])],
            )
    raise AssertionError(op)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(OPS, max_size=12))
def test_random_op_sequences_keep_the_invariants(ops: list[tuple]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = init_project(Path(tmp))
        apm = project.apm_dir
        for op in [*SEED, *ops]:
            before = rev_parse(apm, "HEAD")
            commits = len(log(apm))
            try:
                result = run_op(project, op)
            except InvariantError, OpError:
                # Refused: nothing moved, nothing left behind.
                assert rev_parse(apm, "HEAD") == before
            else:
                if result is None or result.sha is None:
                    assert rev_parse(apm, "HEAD") == before
                else:
                    assert rev_parse(apm, "HEAD") == result.sha
                    assert len(log(apm)) == commits + 1
                    assert log(apm)[0].trailers["APM-Nodes"] == ",".join(result.nodes)
            assert git(apm, ["status", "--porcelain"]) == ""
            graph = load_graph(project)
            assert validate(graph).errors == ()
            bodies = {p.stem for p in (apm / "nodes").glob("*.md")}
            assert bodies == set(graph.nodes)
