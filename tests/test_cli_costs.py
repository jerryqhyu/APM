"""`apm costs`."""

import json

import pytest

from apm.core.costs import live_cost, read_costs
from apm.core.ids import handle
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.store import Store
from tests.test_cli import apm


@pytest.fixture
def cwd(project: Project, monkeypatch: pytest.MonkeyPatch) -> Project:
    monkeypatch.chdir(project.code_dir)
    return project


def test_costs_lists_the_total_and_each_feature_agent_and_day(cwd: Project) -> None:
    search = create_nodes(cwd, [NewNode(title="Search")]).created[0]
    index = create_nodes(cwd, [NewNode(title="Index", parent=search)]).created[0]
    export = create_nodes(cwd, [NewNode(title="Export")]).created[0]
    create_nodes(cwd, [NewNode(title="CSV", parent=export)])
    store = Store(cwd.apm_dir)
    runs = [
        (search, "apm-feature-reviewer", 1.0),
        (index, "apm-implementer", 2.5),
        (export, "apm-critic", 0.25),
        (export, "apm-critic", None),
    ]
    for node_id, agent, cost in runs:
        run = store.create_run(node_id=node_id, agent=agent)
        store.finish_run(run.id, "succeeded", cost_usd=cost)

    data = json.loads(apm("costs", "--json"))
    assert set(data) == {"total", "by_feature", "by_agent", "by_day"}
    assert data["total"] == sum(c for _, _, c in runs if c is not None) == 3.75
    graph = load_graph(cwd)
    frozen = read_costs(cwd.apm_dir)
    for feature in (search, export):
        assert data["by_feature"][feature] == live_cost(graph, store, frozen, feature).total
    assert data["by_agent"] == {
        "apm-critic": 0.25,
        "apm-feature-reviewer": 1.0,
        "apm-implementer": 2.5,
    }
    assert sum(data["by_day"].values()) == 3.75

    text = apm("costs")
    assert text.startswith("Total  $3.75\n")
    for section in ("By feature:", "By agent:", "By day:"):
        assert f"\n{section}\n" in text
    assert f"  {handle(search)} Search  $    3.50\n" in text
    assert f"  {handle(export)} Export  $    0.25\n" in text
    assert "  apm-implementer       $    2.50\n" in text


def test_costs_with_no_runs(cwd: Project) -> None:
    assert json.loads(apm("costs", "--json"))["total"] == 0
    assert "By day:\n  nothing yet\n" in apm("costs")
