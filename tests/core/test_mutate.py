import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from apm.core.git import GitError, git, log, rev_parse
from apm.core.ids import handle
from apm.core.lock import LockTimeoutError, acquire_write_lock
from apm.core.model import Node, Status
from apm.core.mutate import DirtyPlanError, Draft, OpError, commit_hand_edits, mutate
from apm.core.ndjson import GraphParseError
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.validate import InvariantError
from tests.core.build import A, B, node


def head(project: Project) -> str | None:
    return rev_parse(project.apm_dir, "HEAD")


def is_clean(project: Project) -> bool:
    return git(project.apm_dir, ["status", "--porcelain"]) == ""


def add_nodes(*nodes: Node):
    def apply(draft: Draft) -> str:
        for n in nodes:
            draft.put(n)
        return f"{len(nodes)} nodes"

    return apply


def test_commits_the_graph_and_bodies_with_trailers(project: Project) -> None:
    def apply(draft: Draft) -> str:
        draft.put(node(A))
        draft.put(node(B, A))
        draft.set_body(B, "## Intent\n\nShip it.\n")
        return "2 nodes"

    result = mutate(
        project, "create", apply, actor="agent:apm-decomposer", run="run-7", note="Why."
    )

    assert result.sha == head(project)
    assert result.nodes == (A, B)
    assert result.created == (A, B)
    assert result.graph.children(A) == (B,)
    assert is_clean(project)
    assert load_graph(project).nodes == result.graph.nodes
    assert (project.apm_dir / "nodes" / f"{B}.md").read_text() == "## Intent\n\nShip it.\n"
    assert not (project.apm_dir / "nodes" / f"{A}.md").exists()

    entry = log(project.apm_dir)[0]
    assert entry.subject == "create 2 nodes"
    assert entry.trailers == {
        "APM-Op": "create",
        "APM-Nodes": f"{A},{B}",
        "APM-Actor": "agent:apm-decomposer",
        "APM-Run": "run-7",
    }
    assert git(project.apm_dir, ["log", "-1", "--format=%b"]).startswith("Why.\n")
    changed = git(project.apm_dir, ["show", "--name-only", "--format=", "HEAD"]).split()
    assert changed == ["graph.ndjson", f"nodes/{B}.md"]


def test_a_change_that_breaks_an_invariant_writes_nothing(project: Project) -> None:
    mutate(project, "create", add_nodes(node(A)))
    before = head(project)

    def apply(draft: Draft) -> str:
        draft.put(replace(draft[A], depends_on=(A,)))
        draft.set_body(A, "never written")
        return "bad"

    with pytest.raises(InvariantError) as err:
        mutate(project, "link", apply)
    assert [i.code for i in err.value.issues] == ["I2"]
    assert head(project) == before
    assert is_clean(project)
    assert not (project.apm_dir / "nodes").exists()
    assert not (project.apm_dir / ".cache" / "write.lock").exists()


def test_an_operation_that_changes_nothing_makes_no_commit(project: Project) -> None:
    mutate(project, "create", add_nodes(node(A)))
    before = head(project)
    result = mutate(project, "status", lambda draft: draft.put(draft[A]) or "same")
    assert result.sha is None
    assert result.nodes == ()
    assert head(project) == before


def test_removing_a_node_removes_its_body(project: Project) -> None:
    def create(draft: Draft) -> str:
        draft.put(node(A))
        draft.set_body(A, "text")
        return "a"

    mutate(project, "create", create)
    body = project.apm_dir / "nodes" / f"{A}.md"
    assert body.exists()
    result = mutate(project, "delete", lambda draft: draft.remove(A) or "a")
    assert result.nodes == (A,)
    assert not body.exists()
    assert is_clean(project)
    assert len(load_graph(project)) == 0


def test_the_draft_reads_bodies_and_rejects_unknown_nodes(project: Project) -> None:
    seen = {}

    def apply(draft: Draft) -> str:
        draft.put(node(A))
        seen["empty"] = draft.body(A)
        draft.set_body(A, "one")
        seen["pending"] = draft.body(A)
        assert A in draft
        assert B not in draft
        draft[B]
        return "unreachable"

    with pytest.raises(OpError, match="unknown node"):
        mutate(project, "create", apply)
    assert seen == {"empty": "", "pending": "one"}


def test_refuses_to_sweep_up_uncommitted_hand_edits(project: Project) -> None:
    mutate(project, "create", add_nodes(node(A)))
    before = head(project)
    graph_file = project.apm_dir / "graph.ndjson"
    edited = graph_file.read_text().replace("node 0a", "edited by hand")
    graph_file.write_text(edited)

    with pytest.raises(DirtyPlanError) as err:
        mutate(project, "create", add_nodes(node(B)))
    assert err.value.paths == ("graph.ndjson",)
    assert head(project) == before
    assert graph_file.read_text() == edited


def test_hand_edits_to_other_files_are_left_alone(project: Project) -> None:
    def create(draft: Draft) -> str:
        draft.put(node(A))
        draft.set_body(A, "text")
        return "a"

    mutate(project, "create", create)
    body = project.apm_dir / "nodes" / f"{A}.md"
    body.write_text("edited by hand")
    mutate(project, "create", add_nodes(node(B)))
    assert body.read_text() == "edited by hand"
    assert git(project.apm_dir, ["status", "--porcelain"]).split() == ["M", f"nodes/{A}.md"]


def test_a_failed_commit_puts_everything_back(project: Project) -> None:
    def create(draft: Draft) -> str:
        draft.put(node(A))
        draft.set_body(A, "old")
        return "a"

    mutate(project, "create", create)
    before = head(project)
    graph_text = (project.apm_dir / "graph.ndjson").read_text()
    hook = project.apm_dir / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    def change(draft: Draft) -> str:
        draft.put(replace(draft[A], status=Status.DONE))
        draft.put(node(B))
        draft.set_body(A, "new")
        draft.set_body(B, "brand new")
        return "everything"

    with pytest.raises(GitError):
        mutate(project, "update", change)
    assert head(project) == before
    assert is_clean(project)
    assert (project.apm_dir / "graph.ndjson").read_text() == graph_text
    assert (project.apm_dir / "nodes" / f"{A}.md").read_text() == "old"
    assert not (project.apm_dir / "nodes" / f"{B}.md").exists()

    hook.unlink()
    assert mutate(project, "update", change).sha is not None


def test_waits_for_the_write_lock(project: Project) -> None:
    with (
        acquire_write_lock(project.apm_dir),
        pytest.raises(LockTimeoutError),
    ):
        mutate(project, "create", add_nodes(node(A)), timeout=0.1)
    assert mutate(project, "create", add_nodes(node(A))).sha is not None


def test_concurrent_processes_each_get_their_commit(project: Project) -> None:
    workers, each = 4, 5
    script = Path(__file__).with_name("mutate_worker.py")
    children = [
        subprocess.Popen([sys.executable, str(script), str(project.code_dir), f"w{w}", str(each)])
        for w in range(workers)
    ]
    assert [child.wait(timeout=120) for child in children] == [0] * workers

    titles = sorted(n.title for n in load_graph(project).nodes.values())
    assert titles == sorted(f"w{w}-{i}" for w in range(workers) for i in range(each))
    entries = log(project.apm_dir)
    assert len(entries) == workers * each + 1  # plus the init commit
    assert is_clean(project)


def lines(project: Project) -> list[dict]:
    text = (project.apm_dir / "graph.ndjson").read_text()
    return [json.loads(line) for line in text.splitlines()]


def write(project: Project, nodes: list[dict]) -> None:
    text = "".join(json.dumps(n) + "\n" for n in nodes)
    (project.apm_dir / "graph.ndjson").write_text(text)


def test_nothing_to_commit(project: Project) -> None:
    assert commit_hand_edits(project).sha is None


def test_a_graph_edit_is_committed_as_an_update_in_canonical_form(project: Project) -> None:
    a, b = create_nodes(project, [NewNode(title="A"), NewNode(title="B")]).created
    nodes = lines(project)
    nodes[0]["title"] = "A, renamed"
    nodes[1]["depends_on"] = [a]
    write(project, [nodes[1], nodes[0]])  # out of order, with spaces after separators

    result = commit_hand_edits(project)
    assert result.nodes == (a, b)
    graph = load_graph(project)
    assert graph[a].title == "A, renamed"
    assert graph[b].depends_on == (a,)
    entry = log(project.apm_dir)[0]
    assert entry.subject == "update 2 nodes: hand edits"
    assert entry.trailers["APM-Nodes"] == f"{a},{b}"
    assert git(project.apm_dir, ["status", "--porcelain"]) == ""
    assert (project.apm_dir / "graph.ndjson").read_text().startswith(f'{{"id":"{a}","title"')


def test_a_body_edit_is_committed_as_edit_body(project: Project) -> None:
    (a,) = create_nodes(project, [NewNode(title="A")]).created
    (project.apm_dir / "nodes" / f"{a}.md").write_text("## Intent\n\nBy hand.\n")
    result = commit_hand_edits(project)
    assert result.nodes == (a,)
    assert log(project.apm_dir)[0].subject == f"edit-body {handle(a)}: hand edits"


def test_deleting_a_node_by_hand_takes_its_body_along(project: Project) -> None:
    a, b = create_nodes(project, [NewNode(title="A"), NewNode(title="B")]).created
    write(project, [n for n in lines(project) if n["id"] != b])
    commit_hand_edits(project)
    assert set(load_graph(project).nodes) == {a}
    assert not (project.apm_dir / "nodes" / f"{b}.md").exists()
    assert git(project.apm_dir, ["status", "--porcelain"]) == ""


def test_invalid_edits_are_refused_and_left_in_place(project: Project) -> None:
    (a,) = create_nodes(project, [NewNode(title="A")]).created
    before = len(log(project.apm_dir))
    nodes = lines(project)

    write(project, [{**nodes[0], "depends_on": [a]}])
    with pytest.raises(InvariantError, match="I2"):
        commit_hand_edits(project)

    (project.apm_dir / "graph.ndjson").write_text("{not json\n")
    with pytest.raises(GraphParseError, match=r"graph\.ndjson:1: invalid JSON"):
        commit_hand_edits(project)

    write(project, nodes)
    stray = project.apm_dir / "nodes" / "notes.txt"
    stray.write_text("scratch")
    with pytest.raises(OpError, match="not a node body"):
        commit_hand_edits(project)
    stray.unlink()
    orphan = project.apm_dir / "nodes" / "01930000-0000-7000-8000-000000000001.md"
    orphan.write_text("nobody's")
    with pytest.raises(OpError, match="no node has this id"):
        commit_hand_edits(project)

    assert len(log(project.apm_dir)) == before
