import json

import pytest

from apm.core.git import git, log
from apm.core.handedit import commit_hand_edits
from apm.core.ids import handle
from apm.core.mutate import OpError
from apm.core.ndjson import GraphParseError
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.validate import InvariantError


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
