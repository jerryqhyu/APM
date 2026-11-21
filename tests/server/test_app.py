import io
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apm.core.archive import MANIFEST_FILE, PROPOSALS_FILE, RUNS_FILE, import_archive
from apm.core.git import log
from apm.core.ids import handle
from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.project import Project, load_graph
from apm.core.store import Store
from apm.server.app import archive_name, create_app
from apm.server.security import host_is_loopback, origin_allowed
from tests.conftest import tasks_of
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
    # The three, and Ship's review step.
    assert info["counts"] == {"nodes": 4, "ready": 1, "proposals": 0, "running": 0}
    assert [(a["name"], a["label"], a["needs_node"]) for a in info["agents"]] == [
        ("apm-assistant", "Chat", False),
        ("apm-decomposer", "Decompose", True),
        ("apm-implementer", "Implement", True),
        ("apm-feature-reviewer", "Review feature", True),
    ]
    assert info["default_mode"] == "auto"
    assert [m["name"] for m in info["modes"]] == [
        "auto",
        "manual",
        "acceptEdits",
        "plan",
        "dontAsk",
    ]

    tree = client.get("/api/tree").json()
    assert [n["title"] for n in tree["nodes"]] == ["Ship v1", "Build", "Test", "Feature review"]
    assert tree["errors"] == []
    assert tree["head"] == info["head"]

    top = client.get("/api/graph").json()
    assert top["root"] is None
    assert [n["title"] for n in top["nodes"]] == ["Ship v1"]
    assert top["nodes"][0]["progress"] == {"done": 0, "total": 3}  # with its review step

    level = client.get("/api/graph", params={"root": ship_id}).json()
    assert level["root"]["title"] == "Ship v1"
    assert [b["title"] for b in level["breadcrumb"]] == ["Ship v1"]
    assert [n["title"] for n in level["nodes"]] == ["Build", "Test", "Feature review"]
    review = load_graph(project).review_step(ship_id)  # waits on both tasks
    assert sorted((e["src"], e["dst"], e["count"]) for e in level["edges"]) == sorted(
        [(test["id"], build["id"], 1), (review, build["id"], 1), (review, test["id"], 1)]
    )

    assert [n["title"] for n in client.get("/api/ready").json()] == ["Build"]
    assert [h["title"] for h in client.get("/api/search", params={"q": "make"}).json()] == ["Build"]


def test_ready_includes_stacked_work_and_says_so(client: TestClient, project: Project) -> None:
    first = client.post("/api/nodes", json={"title": "First"}).json()["id"]
    second = client.post("/api/nodes", json={"title": "Second", "depends_on": [first]}).json()["id"]
    client.patch(f"/api/nodes/{first}", json={"status": "in_progress"})
    assert client.get("/api/ready").json() == []
    attach_delivery(project, first, branch="apm/first", pr="https://github.com/o/r/pull/20")
    (ready,) = client.get("/api/ready").json()
    assert (ready["id"], ready["ready"], ready["blocked"]) == (second, True, False)
    assert ready["stacked_on"] == [{"id": first, "handle": handle(first), "pr": "#20"}]
    assert client.get("/api/project").json()["counts"]["ready"] == 1


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
    step = load_graph(project).review_step(a)  # B made A a feature, with a review step
    assert step is not None
    assert sorted(
        client.delete(f"/api/nodes/{a}", params={"recursive": True}).json()["deleted"]
    ) == sorted([a, b, step])
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
        json={"agent": "apm-decomposer", "node_id": node["handle"], "message": "Small."},
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
    assert tasks_of(graph, node["id"]) == (child,)
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
        "/api/runs", json={"agent": "apm-assistant", "node_id": child, "message": "Why?"}
    ).json()
    while not client.get(f"/api/runs/{run['id']}").json()["events"]:  # Claude has started
        time.sleep(0.02)
    assert run["mode"] == "auto"
    busy = client.post(f"/api/runs/{run['id']}/continue", json={"message": "more"})
    assert busy.status_code == 409
    assert "already running in this conversation" in busy.json()["error"]
    assert (
        client.post("/api/runs", json={"agent": "apm-assistant", "mode": "yolo"}).status_code == 422
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


# --- merge, the project chat, and questions from a running agent --------------------------------


def test_merge_and_project_actions(client: TestClient, project: Project) -> None:
    a = client.post("/api/nodes", json={"title": "A"}).json()["id"]
    b = client.post("/api/nodes", json={"title": "B"}).json()["id"]
    c = client.post("/api/nodes", json={"title": "C", "depends_on": [b]}).json()["id"]
    before = commits(project)

    merged = client.post("/api/merge", json={"target": a, "others": [b], "title": "A and B"})
    assert merged.status_code == 200
    assert merged.json()["title"] == "A and B"
    assert commits(project) == before + 1
    graph = load_graph(project)
    assert b not in graph
    assert graph[c].depends_on == (a,)
    assert client.post("/api/merge", json={"target": a, "others": [a]}).status_code == 422

    assert client.post("/api/reconcile").json() == {"done": [], "findings": []}
    assert client.post("/api/commit").json() == {"sha": None, "nodes": []}
    detail = client.get(f"/api/nodes/{a}").json()
    assert detail["links"]["dependents"][0]["title"] == "C"
    assert detail["links"]["children"] == []


def test_the_chat_about_the_whole_project(client: TestClient, fake_claude: FakeClaude) -> None:
    started = client.post("/api/runs", json={"agent": "apm-assistant", "message": "Plan it."})
    assert started.status_code == 201
    assert started.json()["node_id"] == ""
    (run,) = wait_for_runs(client)
    assert [r["id"] for r in client.get("/api/runs", params={"scope": "project"}).json()] == [
        run["id"]
    ]
    assert fake_claude.calls[0]["prompt"] == "Plan it."
    assert client.post("/api/runs", json={"agent": "apm-decomposer"}).status_code == 422


def test_a_question_from_a_running_agent_is_answered_over_the_api(
    client: TestClient, project: Project, fake_claude: FakeClaude
) -> None:
    node = create_nodes(project, [NewNode(title="N")]).created[0]
    fake_claude.mode("ask-question", "ask-permission")
    with client.websocket_connect(f"ws://127.0.0.1:4100/ws?token={TOKEN}") as socket:
        socket.receive_json()
        run = client.post(
            "/api/runs", json={"agent": "apm-assistant", "node_id": node, "mode": "manual"}
        ).json()

        def wait_for(kind: str) -> dict[str, Any]:
            while (event := socket.receive_json())["type"] != kind:
                pass
            return event

        # First a permission prompt, which is refused.
        request = wait_for("run.request")["request"]
        assert (request["kind"], request["tool"], request["run_id"]) == (
            "permission",
            "Bash",
            run["id"],
        )
        assert client.get("/api/requests").json() == [request]
        assert client.get(f"/api/runs/{run['id']}").json()["requests"] == [request]
        refused = client.post(
            f"/api/requests/{request['id']}", json={"allow": False, "message": "No."}
        )
        assert refused.json() == {"answered": request["id"]}
        assert wait_for("run.request_resolved")["id"] == request["id"]

        # Then a question, answered with one of its options.
        question = wait_for("run.request")["request"]
        assert question["kind"] == "question"
        client.post(
            f"/api/requests/{question['id']}",
            json={"allow": True, "answers": {"Tabs or spaces?": "Tabs"}},
        )
        assert wait_for("run.finished")["run"]["status"] == "succeeded"

    events = client.get(f"/api/runs/{run['id']}").json()["events"]
    said = [
        e["data"]["message"]["content"][0]["text"]
        for e in events
        if e["data"]["type"] == "assistant"
    ]
    assert said[-2:] == ["denied: No.", "you chose: Tabs"]
    assert client.get("/api/requests").json() == []
    late = client.post(f"/api/requests/{question['id']}", json={"allow": True})
    assert late.status_code == 422
    assert "no longer waiting" in late.json()["error"]


def test_proposals_for_deleted_nodes_close_themselves(client: TestClient, project: Project) -> None:
    node = client.post("/api/nodes", json={"title": "Doomed"}).json()["id"]
    client.post("/api/runs", json={"agent": "apm-decomposer", "node_id": node, "critic": False})
    wait_for_runs(client)
    (proposal,) = client.get("/api/proposals").json()
    client.delete(f"/api/nodes/{node}")
    assert client.get("/api/proposals").json() == []
    assert client.get("/api/project").json()["counts"]["proposals"] == 0
    gone = client.post(f"/api/proposals/{proposal['id']}/accept")
    assert gone.status_code == 409
    assert "no longer exists" in gone.json()["error"]


def test_a_blank_title_is_refused_not_ignored(client: TestClient) -> None:
    node = client.post("/api/nodes", json={"title": "  Kept  "}).json()
    assert node["title"] == "Kept"
    refused = client.patch(f"/api/nodes/{node['id']}", json={"title": "", "status": "done"})
    assert refused.status_code == 422
    assert refused.json()["error"] == "a title can't be blank"
    assert client.get(f"/api/nodes/{node['id']}").json()["status"] == "todo"


# --- archives -----------------------------------------------------------------------------------


def archive_names(content: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return set(archive.namelist())


def test_export_downloads_the_plan_as_an_archive(
    client: TestClient, project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    assert client.get("/api/export", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/export", headers={"Authorization": "Bearer wrong"}).status_code == 401

    response = client.get("/api/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert 'filename="my-app.apm.zip"' in disposition
    names = archive_names(response.content)
    assert MANIFEST_FILE in names
    assert RUNS_FILE not in names
    assert PROPOSALS_FILE not in names
    assert list(scratch.iterdir()) == []  # the temporary copy is cleaned up after sending


def test_export_with_runs_holds_runs_and_proposals(client: TestClient, project: Project) -> None:
    store = Store(project.apm_dir)
    run = store.create_run(node_id="", agent="apm-decomposer")
    store.add_event(run.id, {"type": "assistant"})
    store.finish_run(run.id, "succeeded")
    store.create_proposal(node_id="", data={"children": []}, run_id=run.id)

    assert {RUNS_FILE, PROPOSALS_FILE} <= archive_names(
        client.get("/api/export", params={"with_runs": "true"}).content
    )
    assert not {RUNS_FILE, PROPOSALS_FILE} & archive_names(
        client.get("/api/export", params={"with_runs": "false"}).content
    )


def test_a_downloaded_archive_imports(client: TestClient, project: Project, tmp_path: Path) -> None:
    node = client.post("/api/nodes", json={"title": "Carried over"}).json()["id"]
    store = Store(project.apm_dir)
    store.finish_run(store.create_run(node_id=node, agent="apm-implementer").id, "succeeded")
    archive = tmp_path / "download.apm.zip"
    archive.write_bytes(client.get("/api/export", params={"with_runs": "true"}).content)
    target = tmp_path / "elsewhere"
    target.mkdir()

    imported = import_archive(archive, target)

    graph = load_graph(imported.project)
    assert [graph.nodes[n].title for n in graph.nodes] == ["Carried over"]
    assert [r.node_id for r in Store(imported.project.apm_dir).list_runs()] == [node]
    assert imported.manifest["counts"]["runs"] == 1


def test_archive_names_are_safe_file_names() -> None:
    assert archive_name("my-app") == "my-app.apm.zip"
    assert archive_name("My App/v2") == "My-App-v2.apm.zip"
    assert archive_name("../..") == "plan.apm.zip"
    assert archive_name("café") == "café.apm.zip"
