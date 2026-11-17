import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apm.core.git import log
from apm.core.ids import handle
from apm.core.model import Status
from apm.core.ops import NewNode, create_nodes, set_status
from apm.core.project import Project, load_graph
from apm.server.app import create_app
from apm.server.security import host_is_loopback, origin_allowed
from tests.runner.fake import FakeClaude

TOKEN = "t" * 43
BASE = "http://127.0.0.1:4100"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(project: Project, fake_claude: FakeClaude) -> Iterator[TestClient]:
    app = create_app(
        project, TOKEN, static_dir=None, extra_origins=frozenset({"http://localhost:5173"})
    )
    with TestClient(app, base_url=BASE, headers=AUTH) as test_client:
        yield test_client


def commits(project: Project) -> int:
    return len(log(project.apm_dir))


def wait_for_runs(client: TestClient, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Polls until no run is running, then returns all runs, oldest first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = client.get("/api/runs").json()
        if runs and not any(r["status"] == "running" for r in runs):
            # The proposal is written just after the last run finishes.
            time.sleep(0.1)
            return sorted(client.get("/api/runs").json(), key=lambda r: r["started"])
        time.sleep(0.05)
    raise AssertionError("runs did not finish")


# --- security -----------------------------------------------------------------------------------


def test_api_needs_the_token(client: TestClient) -> None:
    assert client.get("/api/project", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/project", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.post("/api/nodes", json={"title": "x"}, headers={"Authorization": ""}).status_code
        == 401
    )
    assert "token" in client.get("/api/project", headers={"Authorization": ""}).json()["error"]
    assert client.get("/api/project").status_code == 200


def test_requests_from_other_sites_are_refused(client: TestClient) -> None:
    assert client.get("/api/project", headers={"Origin": "https://evil.example"}).status_code == 403
    assert (
        client.get("/api/project", headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    )
    assert client.get("/api/project", headers={"Origin": BASE}).status_code == 200
    assert (
        client.get("/api/project", headers={"Origin": "http://localhost:5173"}).status_code == 200
    )


def test_a_rebound_host_name_is_refused(client: TestClient) -> None:
    assert client.get("/api/project", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example:4100"}).status_code == 403
    assert client.get("/api/project", headers={"Host": "localhost:4100"}).status_code == 200


def test_the_page_itself_needs_no_token(client: TestClient) -> None:
    response = client.get("/", headers={"Authorization": ""})
    assert response.status_code == 200
    assert "The web UI isn't built yet" in response.text


def test_the_websocket_needs_the_token(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("ws://127.0.0.1:4100/ws?token=wrong"),
    ):
        pass
    evil = {"Origin": "https://evil.example"}
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"ws://127.0.0.1:4100/ws?token={TOKEN}", headers=evil),
    ):
        pass
    with client.websocket_connect(f"ws://127.0.0.1:4100/ws?token={TOKEN}") as socket:
        assert socket.receive_json()["type"] == "hello"


def test_host_and_origin_rules() -> None:
    assert host_is_loopback("127.0.0.1:4100")
    assert host_is_loopback("localhost")
    assert host_is_loopback("[::1]:4100")
    assert not host_is_loopback("example.com")
    assert not host_is_loopback("127.0.0.1.example.com:80")
    assert not host_is_loopback(None)
    none: frozenset[str] = frozenset()
    assert origin_allowed(None, "127.0.0.1:4100", none)
    assert origin_allowed("http://127.0.0.1:4100", "127.0.0.1:4100", none)
    assert not origin_allowed("https://127.0.0.1:4100", "127.0.0.1:4100", none)
    assert not origin_allowed("http://localhost:4100", "127.0.0.1:4100", none)
    assert not origin_allowed("null", "127.0.0.1:4100", none)


# --- the plan -----------------------------------------------------------------------------------


def test_build_and_read_a_plan(client: TestClient, project: Project) -> None:
    ship = client.post("/api/nodes", json={"title": "Ship v1"})
    assert ship.status_code == 201
    ship_id = ship.json()["id"]
    build = client.post(
        "/api/nodes", json={"title": "Build", "parent": ship_id, "intent": "Make it."}
    ).json()
    test = client.post(
        "/api/nodes",
        json={"title": "Test", "parent": ship.json()["handle"], "depends_on": [build["handle"]]},
    ).json()
    assert commits(project) == 4  # init + one per node
    assert "Make it." in build["body"]
    assert test["blocked"]
    assert test["links"]["waiting_on"][0]["title"] == "Build"
    assert [a["title"] for a in test["ancestors"]] == ["Ship v1"]
    assert test["history"][0]["subject"] == f"create {test['handle']}: Test"

    info = client.get("/api/project").json()
    assert info["name"] == "my-app"
    assert info["counts"] == {"nodes": 3, "ready": 1, "proposals": 0, "running": 0}
    assert [a["name"] for a in info["agents"]] == [
        "apm-decomposer",
        "apm-implementer",
        "apm-assistant",
    ]

    tree = client.get("/api/tree").json()
    assert [n["title"] for n in tree["nodes"]] == ["Ship v1", "Build", "Test"]
    assert tree["errors"] == []
    assert tree["head"] == info["head"]

    top = client.get("/api/graph").json()
    assert top["root"] is None
    assert [n["title"] for n in top["nodes"]] == ["Ship v1"]
    assert top["nodes"][0]["progress"] == {"done": 0, "total": 2}

    level = client.get("/api/graph", params={"root": ship_id}).json()
    assert level["root"]["title"] == "Ship v1"
    assert [b["title"] for b in level["breadcrumb"]] == ["Ship v1"]
    assert [n["title"] for n in level["nodes"]] == ["Build", "Test"]
    assert [(e["src"], e["dst"], e["count"]) for e in level["edges"]] == [
        (test["id"], build["id"], 1)
    ]

    assert [n["title"] for n in client.get("/api/ready").json()] == ["Build"]
    assert [h["title"] for h in client.get("/api/search", params={"q": "make"}).json()] == ["Build"]


def test_each_edit_is_one_commit(client: TestClient, project: Project) -> None:
    a = client.post("/api/nodes", json={"title": "A"}).json()["id"]
    b = client.post("/api/nodes", json={"title": "B"}).json()["id"]
    start = commits(project)

    node = client.patch(f"/api/nodes/{a}", json={"title": "Alpha"}).json()
    assert node["title"] == "Alpha"
    node = client.patch(
        f"/api/nodes/{a}", json={"status": "in_progress", "note": "Started."}
    ).json()
    assert node["status"] == "in_progress"
    node = client.patch(f"/api/nodes/{a}", json={"body": "## Intent\n\nNew.\n"}).json()
    assert node["body"] == "## Intent\n\nNew.\n"
    assert commits(project) == start + 3
    assert client.patch(f"/api/nodes/{a}", json={"title": "Alpha"}).status_code == 200
    assert commits(project) == start + 3  # nothing changed, nothing committed

    assert client.post("/api/edges", json={"src": a, "dst": b}).json() == {"changed": True}
    assert client.post("/api/edges", json={"src": a, "dst": b}).json() == {"changed": False}
    assert client.get(f"/api/nodes/{a}").json()["depends_on"] == [b]
    assert client.delete("/api/edges", params={"src": a, "dst": b}).json() == {"changed": True}

    moved = client.post(f"/api/nodes/{b}/move", json={"parent": a}).json()
    assert moved["parent"] == a
    assert client.post(f"/api/nodes/{b}/move", json={"parent": None}).json()["parent"] is None
    assert commits(project) == start + 7

    client.post(f"/api/nodes/{b}/move", json={"parent": a})
    refused = client.delete(f"/api/nodes/{a}")
    assert refused.status_code == 422
    assert "has 1 child" in refused.json()["error"]
    assert sorted(
        client.delete(f"/api/nodes/{a}", params={"recursive": True}).json()["deleted"]
    ) == sorted([a, b])
    assert len(load_graph(project)) == 0


def test_errors_have_a_status_and_a_message(client: TestClient) -> None:
    a = client.post("/api/nodes", json={"title": "A"}).json()["id"]
    b = client.post("/api/nodes", json={"title": "B", "depends_on": [a]}).json()["id"]
    assert client.get("/api/nodes/abcd1234").status_code == 404
    assert "no node matches" in client.get("/api/nodes/abcd1234").json()["error"]
    cycle = client.post("/api/edges", json={"src": a, "dst": b})
    assert cycle.status_code == 422
    assert cycle.json()["error"].startswith("I2: dependency cycle")
    assert client.post("/api/nodes", json={"title": "  "}).status_code == 422
    assert client.post("/api/nodes", json={}).status_code == 422
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope").json() == {"error": "unknown run nope"}
    assert client.get("/api/proposals/nope").status_code == 404


# --- runs and proposals -------------------------------------------------------------------------


def test_decompose_then_accept_an_edited_proposal(client: TestClient, project: Project) -> None:
    node = client.post("/api/nodes", json={"title": "File I/O"}).json()
    started = client.post(
        "/api/runs",
        json={"agent": "apm-decomposer", "node_id": node["handle"], "guidance": "Small."},
    )
    assert started.status_code == 201
    assert started.json()["status"] == "running"

    decomposer, critic = wait_for_runs(client)
    assert (decomposer["agent"], decomposer["status"]) == ("apm-decomposer", "succeeded")
    assert critic["parent_run"] == decomposer["id"]
    detail = client.get(f"/api/runs/{decomposer['id']}").json()
    assert [e["data"]["type"] for e in detail["events"]] == ["system", "assistant", "result"]
    assert (
        client.get(f"/api/runs/{decomposer['id']}", params={"after": 2}).json()["events"][0]["seq"]
        == 3
    )

    (proposal,) = client.get("/api/proposals").json()
    assert proposal["node_id"] == node["id"]
    assert proposal["review"]["verdict"] == "accept"
    assert client.get(f"/api/nodes/{node['id']}").json()["proposals"][0]["id"] == proposal["id"]
    assert client.get("/api/project").json()["counts"]["proposals"] == 1

    # Keep only the first child, renamed.
    kept = {**proposal["data"]["children"][0], "title": "Parse the file"}
    before = commits(project)
    accepted = client.post(f"/api/proposals/{proposal['id']}/accept", json={"children": [kept]})
    assert accepted.status_code == 200
    (child,) = accepted.json()["created"]
    graph = load_graph(project)
    assert graph[child].title == "Parse the file"
    assert graph.children(node["id"]) == (child,)
    assert commits(project) == before + 1
    entry = log(project.apm_dir)[0]
    assert entry.subject == f"decompose {node['handle']}: 1 child"
    assert entry.trailers["APM-Actor"] == "agent:apm-decomposer"
    assert entry.trailers["APM-Run"] == decomposer["id"]

    again = client.post(f"/api/proposals/{proposal['id']}/accept")
    assert again.status_code == 409
    assert "already accepted" in again.json()["error"]
    assert client.get("/api/proposals").json() == []
    assert (
        client.get("/api/proposals", params={"status": "accepted"}).json()[0]["id"]
        == proposal["id"]
    )


def test_a_proposal_that_cannot_apply_stays_pending(client: TestClient, project: Project) -> None:
    node = client.post("/api/nodes", json={"title": "File I/O"}).json()
    client.post(
        "/api/runs", json={"agent": "apm-decomposer", "node_id": node["id"], "critic": False}
    )
    wait_for_runs(client)
    (proposal,) = client.get("/api/proposals").json()

    broken = [{**proposal["data"]["children"][0], "depends_on_keys": ["nope"]}]
    refused = client.post(f"/api/proposals/{proposal['id']}/accept", json={"children": broken})
    assert refused.status_code == 422
    assert 'unknown key "nope"' in refused.json()["error"]
    assert client.get(f"/api/proposals/{proposal['id']}").json()["status"] == "pending"
    assert load_graph(project).children(node["id"]) == ()

    assert client.post(f"/api/proposals/{proposal['id']}/reject").json()["status"] == "rejected"
    assert client.post(f"/api/proposals/{proposal['id']}/reject").status_code == 409
    assert load_graph(project).children(node["id"]) == ()


def test_run_limits_refusals_cancel_and_continue(
    client: TestClient, project: Project, fake_claude: FakeClaude
) -> None:
    parent = create_nodes(project, [NewNode(title="Parent")]).created[0]
    child = create_nodes(project, [NewNode(title="Child", parent=parent)]).created[0]
    assert (
        client.post("/api/runs", json={"agent": "apm-implementer", "node_id": parent}).status_code
        == 422
    )
    assert client.post("/api/runs", json={"agent": "nope", "node_id": parent}).status_code == 422
    assert (
        client.post("/api/runs", json={"agent": "apm-assistant", "node_id": "abcd1234"}).status_code
        == 404
    )

    fake_claude.mode("hang")
    run = client.post(
        "/api/runs", json={"agent": "apm-assistant", "node_id": child, "guidance": "Why?"}
    ).json()
    busy = client.post("/api/runs", json={"agent": "apm-assistant", "node_id": child})
    assert busy.status_code == 409
    assert "already active on this node" in busy.json()["error"]
    assert (
        client.post(f"/api/runs/{run['id']}/continue", json={"message": "more"}).status_code == 409
    )
    assert client.get("/api/runs", params={"status": "running"}).json()[0]["id"] == run["id"]

    cancelled = client.delete(f"/api/runs/{run['id']}")
    assert cancelled.json()["status"] == "cancelled"

    fake_claude.mode()
    follow_up = client.post(f"/api/runs/{run['id']}/continue", json={"message": "And then?"})
    assert follow_up.status_code == 201
    runs = wait_for_runs(client)
    assert (runs[-1]["status"], runs[-1]["parent_run"], runs[-1]["prompt"]) == (
        "succeeded",
        run["id"],
        "And then?",
    )
    assert [r["id"] for r in client.get("/api/runs", params={"node_id": child}).json()] == [
        runs[-1]["id"],
        run["id"],
    ]


# --- live events --------------------------------------------------------------------------------


def test_the_websocket_reports_commits_made_by_anyone(client: TestClient, project: Project) -> None:
    node = create_nodes(project, [NewNode(title="N")]).created[0]
    with client.websocket_connect(f"ws://127.0.0.1:4100/ws?token={TOKEN}") as socket:
        assert socket.receive_json()["type"] == "hello"
        result = set_status(project, node, Status.DONE)  # as the CLI or an agent would
        event = socket.receive_json()
        assert event == {"type": "graph.changed", "head": result.sha}


def test_the_websocket_streams_a_run(client: TestClient, project: Project) -> None:
    node = create_nodes(project, [NewNode(title="N")]).created[0]
    with client.websocket_connect(f"ws://127.0.0.1:4100/ws?token={TOKEN}") as socket:
        socket.receive_json()
        client.post("/api/runs", json={"agent": "apm-decomposer", "node_id": node, "critic": False})
        seen = []
        while not seen or seen[-1]["type"] != "proposal.created":
            seen.append(socket.receive_json())
    assert [e["type"] for e in seen] == [
        "run.started",
        "run.event",
        "run.event",
        "run.event",
        "run.finished",
        "proposal.created",
    ]
    assert seen[1]["event"]["data"]["type"] == "system"
    assert seen[4]["run"]["status"] == "succeeded"
    assert seen[5]["proposal"]["node_id"] == node
    assert handle(node) in seen[0]["run"]["prompt"]
