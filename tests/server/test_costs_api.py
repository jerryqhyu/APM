"""The whole plan's cost over the API."""

from fastapi.testclient import TestClient

from apm.core.costs import live_cost, read_costs
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.store import Store
from apm.server.app import create_app
from tests.server.test_app import AUTH, BASE, TOKEN


def test_costs_sum_every_run_and_each_feature_is_its_live_cost(project: Project) -> None:
    search, login = create_nodes(project, [NewNode(title="Search"), NewNode(title="Login")]).created
    kids = [NewNode(title="Index", parent=search), NewNode(title="Form", parent=login)]
    index, form = create_nodes(project, kids).created
    store = Store(project.apm_dir)
    for node_id, agent, cost in [
        (search, "apm-feature-reviewer", 1.0),
        (index, "apm-implementer", 2.5),
        (form, "apm-implementer", 0.5),
    ]:
        run = store.create_run(node_id=node_id, agent=agent)
        store.finish_run(run.id, "succeeded", cost_usd=cost)

    app = create_app(project, TOKEN, static_dir=None)
    with TestClient(app, base_url=BASE, headers=AUTH) as client:
        costs = client.get("/api/costs").json()

    assert costs["total"] == 4.0 == sum(r.cost_usd or 0.0 for r in store.list_runs())
    assert costs["by_agent"] == {"apm-feature-reviewer": 1.0, "apm-implementer": 3.0}
    assert [d["total"] for d in costs["by_day"]] == [4.0]
    graph = load_graph(project)
    assert costs["by_feature"] == [
        {"id": search, "handle": graph.handles[search], "title": "Search", "total": 3.5},
        {"id": login, "handle": graph.handles[login], "title": "Login", "total": 0.5},
    ]
    frozen = read_costs(project.apm_dir)
    for feature in costs["by_feature"]:
        assert feature["total"] == live_cost(graph, store, frozen, feature["id"]).total
