import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from apm.core.files import read_body
from apm.core.git import log
from apm.core.ids import handle
from apm.core.model import Delivery, Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.project import Project, load_graph
from apm.core.review_lock import take_review_lock
from apm.mcp.server import SCOPE_ENV, Scope, build_server

READ_TOOLS = {"apm_get_graph", "apm_get_node", "apm_search", "apm_ready", "apm_check"}
WRITE_TOOLS = {
    "apm_create_nodes",
    "apm_update_node",
    "apm_move_node",
    "apm_link",
    "apm_unlink",
    "apm_set_status",
    "apm_attach_delivery",
    "apm_delete_node",
    "apm_merge_nodes",
}


class Session:
    """An MCP server for one project and scope, called in-process."""

    def __init__(self, project: Project, scope: str | None = None, **env: str) -> None:
        if scope:
            env[SCOPE_ENV] = scope
        self.server = build_server(project, env=env)

    def call(self, tool: str, **arguments: object) -> str:
        result = asyncio.run(self.server.call_tool(tool, arguments))
        assert isinstance(result, CallToolResult)
        (content,) = result.content
        assert isinstance(content, TextContent)
        return content.text

    def refused(self, tool: str, **arguments: object) -> str:
        with pytest.raises(ToolError) as err:
            self.call(tool, **arguments)
        return str(err.value)


@pytest.fixture
def plan(project: Project) -> tuple[Project, str, str, str]:
    """Parent ─ First, Second (Second depends on First). Returns the project and three handles."""
    parent = create_nodes(project, [NewNode(title="Parent")]).created[0]
    first, second = create_nodes(
        project,
        [
            NewNode(title="First", parent=parent, key="a"),
            NewNode(title="Second", parent=parent, depends_on_keys=["a"]),
        ],
    ).created
    return project, handle(parent), handle(first), handle(second)


def review_of(project: Project, feature: str) -> str:
    """The handle of `feature`'s review step, which APM added."""
    graph = load_graph(project)
    step = graph.review_step(graph.resolve(feature))
    assert step is not None
    return handle(step)


def test_lists_every_tool_with_a_description(project: Project) -> None:
    tools = asyncio.run(Session(project).server.list_tools())
    assert {t.name for t in tools} == READ_TOOLS | WRITE_TOOLS
    assert all(t.description for t in tools)


def test_read_tools(plan: tuple[Project, str, str, str]) -> None:
    project, parent, first, second = plan
    session = Session(project, "read")
    review = review_of(project, parent)
    assert session.call("apm_get_graph").splitlines() == [
        f"{parent} [work/todo·ready] Parent (0/3)",
        f"  {first} [work/todo·ready] First",
        f"  {second} [work/todo·blocked] Second → {first}",
        f"  {review} [review/todo·blocked] Feature review → {first}, {second}",
    ]
    assert session.call("apm_get_graph", root=parent, depth=1).splitlines() == [
        f"{first} [work/todo·ready] First",
        f"{second} [work/todo·blocked] Second → {first}",
        f"{review} [review/todo·blocked] Feature review → {first}, {second}",
    ]
    shown = session.call("apm_get_node", node=second)
    assert shown.splitlines()[0] == f"{second} [work/todo·blocked] Second → {first}"
    assert f"parent: {parent} [work/todo·ready] Parent (0/3)" in shown
    assert f"waiting on: {first} [work/todo·ready] First" in shown
    assert "## Intent" in shown
    assert session.call("apm_ready") == f"{first} [work/todo·ready] First"
    assert session.call("apm_ready", root=second) == "(nothing is ready)"
    assert session.call("apm_search", query="second") == f"{second} Second"
    assert session.call("apm_search", query="zebra") == "(no matches)"
    assert session.call("apm_check") == "ok"


def test_check_also_checks_apm_yaml(plan: tuple[Project, str, str, str]) -> None:
    project, parent, _, _ = plan
    config = project.apm_dir / "apm.yaml"
    text = config.read_text()
    config.write_text(f"{text}features:\n  overrides:\n    abcdef12: {{ eager: true }}\n")
    session = Session(project, "read")
    assert session.call("apm_check") == (
        "warning apm.yaml: features.overrides.abcdef12 matches no node: it is ignored"
    )
    config.write_text(f"{text}features:\n  overrides:\n    {parent}: {{ eager: maybe }}\n")
    assert session.call("apm_check").startswith(
        f"error apm.yaml: features.overrides.{parent}.eager: "
    )


def test_ready_marks_stacked_work(plan: tuple[Project, str, str, str]) -> None:
    project, _, first, second = plan
    first_id = load_graph(project).resolve(first)
    set_status(project, first_id, Status.IN_PROGRESS)
    attach_delivery(project, first_id, branch="apm/first")
    assert Session(project).call("apm_ready") == (
        f"{second} [work/todo·ready·stacked] Second → {first} · stacked on {first}"
    )


def test_an_empty_plan_reads_cleanly(project: Project) -> None:
    assert Session(project).call("apm_get_graph") == "(the plan has no nodes yet)"


def test_project_scope_can_write_everything(plan: tuple[Project, str, str, str]) -> None:
    project, parent, first, second = plan
    session = Session(project, APM_ACTOR="agent:apm-test", APM_RUN="run-9")

    created = session.call(
        "apm_create_nodes",
        nodes=[
            {
                "title": "Third",
                "parent": parent,
                "key": "t",
                "intent": "Why.",
                "acceptance": ["ok"],
            },
            {
                "title": "Choice",
                "kind": "decision",
                "depends_on_keys": ["t"],
                "depends_on": [first],
            },
        ],
    )
    third, choice = (line.split()[0] for line in created.splitlines())
    graph = load_graph(project)
    ids = {handle(n): n for n in graph.nodes}
    assert graph[ids[third]].parent == ids[parent]
    assert graph[ids[choice]].depends_on == tuple(sorted([ids[first], ids[third]]))
    assert "- [ ] ok" in read_body(project.apm_dir, ids[third])
    entry = log(project.apm_dir)[0]
    assert entry.trailers["APM-Actor"] == "agent:apm-test"
    assert entry.trailers["APM-Run"] == "run-9"

    assert session.call("apm_update_node", node=third, title="Third!", body="New body.") == (
        "updated fields and body"
    )
    assert session.call("apm_update_node", node=third, title="Third!") == "nothing to change"
    assert session.call("apm_move_node", node=choice, parent=parent) == "moved"
    assert session.call("apm_move_node", node=choice) == "moved"
    assert session.call("apm_link", node=third, depends_on=second) == "linked"
    assert session.call("apm_unlink", node=third, depends_on=second) == "unlinked"
    assert session.call("apm_set_status", node=first, status="done", note="Done.") == (
        f"{first}: done"
    )
    assert session.call("apm_attach_delivery", node=first, branch="apm/first") == (
        "delivery recorded"
    )
    graph = load_graph(project)
    assert graph[ids[first]].status is Status.DONE
    assert graph[ids[first]].delivery == Delivery(repo="my-app", branch="apm/first")


def test_project_scope_can_merge_and_delete(plan: tuple[Project, str, str, str]) -> None:
    project, parent, first, _ = plan
    session = Session(project)
    extra = session.call("apm_create_nodes", nodes=[{"title": "Extra", "parent": parent}]).split()[
        0
    ]
    merged = session.call("apm_merge_nodes", target=first, others=[extra], title="First and extra")
    assert merged == f"merged 2 nodes into {first}"
    graph = load_graph(project)
    ids = {handle(n): n for n in graph.nodes}
    assert extra not in ids
    assert graph[ids[first]].title == "First and extra"

    assert "has 2 children" in session.refused("apm_delete_node", node=parent)
    assert session.call("apm_delete_node", node=parent, recursive=True) == "deleted"
    assert len(load_graph(project)) == 0


def test_read_scope_refuses_every_write(plan: tuple[Project, str, str, str]) -> None:
    project, parent, first, second = plan
    session = Session(project, "read")
    before = len(log(project.apm_dir))
    calls = {
        "apm_create_nodes": {"nodes": [{"title": "X"}]},
        "apm_update_node": {"node": first, "title": "X"},
        "apm_move_node": {"node": first},
        "apm_link": {"node": parent, "depends_on": first},
        "apm_unlink": {"node": second, "depends_on": first},
        "apm_set_status": {"node": first, "status": "done"},
        "apm_attach_delivery": {"node": first, "branch": "x"},
        "apm_delete_node": {"node": second},
        "apm_merge_nodes": {"target": first, "others": [second]},
    }
    assert calls.keys() == WRITE_TOOLS
    for tool, arguments in calls.items():
        assert "this session is read-only" in session.refused(tool, **arguments)
    assert "read-only" in session.refused("apm_update_node", node=first, body="x")
    assert len(log(project.apm_dir)) == before


def test_node_scope_allows_only_its_own_status_delivery_and_body(
    plan: tuple[Project, str, str, str],
) -> None:
    project, parent, first, second = plan
    ids = {handle(n): n for n in load_graph(project).nodes}
    session = Session(project, f"node:{ids[first]}")

    assert session.call("apm_set_status", node=first, status="in_progress") == (
        f"{first}: in_progress"
    )
    assert session.call("apm_attach_delivery", node=first, branch="apm/first") == (
        "delivery recorded"
    )
    assert session.call("apm_update_node", node=first, body="Notes.") == "updated body"
    assert session.call("apm_get_graph").count("\n") == 3  # reading is never restricted

    message = f"may only update status, delivery and body of {ids[first]}"
    assert message in session.refused("apm_set_status", node=second, status="done")
    assert message in session.refused("apm_attach_delivery", node=second, branch="x")
    assert message in session.refused("apm_update_node", node=second, body="x")
    assert message in session.refused("apm_update_node", node=first, title="Renamed")
    assert message in session.refused("apm_create_nodes", nodes=[{"title": "X"}])
    assert message in session.refused("apm_move_node", node=first)
    assert message in session.refused("apm_link", node=first, depends_on=parent)
    assert message in session.refused("apm_unlink", node=second, depends_on=first)
    assert message in session.refused("apm_delete_node", node=first)
    assert message in session.refused("apm_merge_nodes", target=first, others=[second])


def test_errors_come_back_as_tool_errors(plan: tuple[Project, str, str, str]) -> None:
    project, parent, first, second = plan
    session = Session(project)
    assert "no node matches" in session.refused("apm_get_node", node="abcdef12")
    assert "I2: dependency cycle" in session.refused("apm_link", node=first, depends_on=second)
    assert "I7" in session.refused("apm_attach_delivery", node=parent, branch="x")


def test_get_node_defaults_to_the_node_the_run_is_about(
    plan: tuple[Project, str, str, str],
) -> None:
    project, _, first, second = plan
    ids = {handle(n): n for n in load_graph(project).nodes}
    focused = Session(project, "read", APM_NODE=ids[second])
    assert focused.call("apm_get_node").splitlines()[1] == f"id: {ids[second]}"
    assert focused.call("apm_get_node", node=first).splitlines()[1] == f"id: {ids[first]}"
    assert "which node?" in Session(project, "read").refused("apm_get_node")


def test_scope_parsing() -> None:
    node_id = "01930000-0000-7000-8000-00000000000a"
    assert Scope.parse(None) == Scope("project")
    assert Scope.parse("project") == Scope("project")
    assert Scope.parse("read") == Scope("read")
    assert Scope.parse(f"node:{node_id}") == Scope("node", node_id)
    assert str(Scope.parse(f"node:{node_id}")) == f"node:{node_id}"
    for bad in ("write", "node:", "node:1234", "READ"):
        with pytest.raises(ValueError, match="APM_RUN_SCOPE must be"):
            Scope.parse(bad)


def test_a_subtree_is_one_create_call(project: Project) -> None:
    session = Session(project)
    created = session.call(
        "apm_create_nodes",
        nodes=[
            {"title": "Feature", "key": "f"},
            {"title": "Model", "key": "m", "parent_key": "f"},
            {"title": "View", "parent_key": "f", "depends_on_keys": ["m"]},
        ],
    )
    feature, model, view = (line.split()[0] for line in created.splitlines())
    assert session.call("apm_get_graph").splitlines() == [
        f"{feature} [work/todo·ready] Feature (0/3)",
        f"  {model} [work/todo·ready] Model",
        f"  {view} [work/todo·blocked] View → {model}",
        f"  {review_of(project, feature)} [review/todo·blocked] Feature review → {model}, {view}",
    ]
    assert len(log(project.apm_dir)) == 2  # init + one commit
    assert "a title can't be blank" in session.refused("apm_update_node", node=feature, title=" ")


def test_a_task_under_review_is_refused(plan: tuple[Project, str, str, str]) -> None:
    project, _, first, _ = plan
    take_review_lock(project.apm_dir, load_graph(project).resolve(first), "run-1")
    session = Session(project)
    assert f"{first} is under review" in session.refused("apm_update_node", node=first, title="X")
    session.call("apm_set_status", node=first, status="in_progress")
