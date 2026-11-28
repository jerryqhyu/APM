"""The settings in apm.yaml, and the feature-level buttons, over the API."""

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apm.core.files import read_body
from apm.core.git import log
from apm.core.model import NodeId
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.store import Store, TaskReview, content_fingerprint
from apm.server.app import create_app
from tests.runner.fake import FakeClaude
from tests.server.test_app import AUTH, BASE, TOKEN


@pytest.fixture
def client(project: Project, fake_claude: FakeClaude) -> Iterator[TestClient]:
    # Only what a test starts runs: no automatic task reviews in between.
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + "automation:\n  task_review: false\n")
    app = create_app(project, TOKEN, static_dir=None)
    with TestClient(app, base_url=BASE, headers=AUTH) as test_client:
        yield test_client


def feature(project: Project, *titles: str) -> tuple[NodeId, list[NodeId]]:
    top = create_nodes(project, [NewNode(title="Search")]).created[0]
    kids = create_nodes(project, [NewNode(title=t, parent=top) for t in titles]).created
    return top, list(kids)


def passed(project: Project, node_id: NodeId) -> None:
    node = load_graph(project)[node_id]
    fingerprint = content_fingerprint(node, read_body(project.apm_dir, node_id))
    Store(project.apm_dir).add_task_review(TaskReview(node_id, fingerprint, "pass", {}))


def settled_runs(client: TestClient, count: int, timeout: float = 20.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = client.get("/api/runs").json()
        if len(runs) == count and all(r["status"] != "running" for r in runs):
            return runs
        time.sleep(0.05)
    raise AssertionError(f"expected {count} finished runs: {client.get('/api/runs').json()}")


def test_project_settings_are_read_and_changed(client: TestClient, project: Project) -> None:
    settings = client.get("/api/settings").json()
    assert settings["features"] == {
        "eager": False,
        "task_max_lines": 50,
        "report": True,
        "report_detail": "standard",
        "budget_usd": None,
    }
    assert settings["automation"]["eager_redo"] is True
    assert settings["defaults"]["features"]["task_max_lines"] == 50
    assert settings["overrides"] == []

    changed = client.patch(
        "/api/settings",
        json={
            "features": {"eager": True, "task_max_lines": 80},
            "automation": {"reconcile": False},
        },
    )
    assert changed.status_code == 200
    assert changed.json()["features"]["task_max_lines"] == 80
    assert changed.json()["automation"]["reconcile"] is False
    assert log(project.apm_dir)[0].trailers["APM-Op"] == "settings"
    assert "task_max_lines: 80" in (project.apm_dir / "apm.yaml").read_text()


def test_a_bad_setting_is_refused_with_a_clear_message(
    client: TestClient, project: Project
) -> None:
    before = (project.apm_dir / "apm.yaml").read_text()
    refused = client.patch("/api/settings", json={"features": {"task_max_lines": 2}})
    assert refused.status_code == 422
    assert "features.task_max_lines" in refused.json()["error"]
    unknown = client.patch("/api/settings", json={"features": {"eagre": True}})
    assert unknown.status_code == 422
    assert "unknown setting features.eagre" in unknown.json()["error"]
    assert (project.apm_dir / "apm.yaml").read_text() == before


def test_a_features_own_settings_are_shown_set_and_cleared(
    client: TestClient, project: Project
) -> None:
    top, (task,) = feature(project, "Index")
    detail = client.get(f"/api/nodes/{top}").json()
    defaults = {
        "values": {
            "eager": False,
            "task_max_lines": 50,
            "report": True,
            "report_detail": "standard",
            "budget_usd": None,
        },
        "sources": dict.fromkeys(
            ("eager", "task_max_lines", "report", "report_detail", "budget_usd")
        ),
    }
    assert detail["settings"] == {**defaults, "own": {}, "inherited": defaults}

    own = client.patch(f"/api/nodes/{top}/settings", json={"eager": True, "task_max_lines": 30})
    assert own.status_code == 200
    assert own.json()["own"] == {"eager": True, "task_max_lines": 30}
    assert own.json()["values"]["task_max_lines"] == 30
    assert own.json()["inherited"] == defaults
    inside = client.get(f"/api/nodes/{task}").json()["settings"]
    assert inside["values"]["task_max_lines"] == 30
    assert inside["sources"]["task_max_lines"]["id"] == top
    assert inside["sources"]["report"] is None
    assert client.get("/api/settings").json()["overrides"] == [
        {"key": top, "node": top, "values": {"eager": True, "task_max_lines": 30}}
    ]

    inherited = client.patch(f"/api/nodes/{top}/settings", json={"eager": None}).json()
    assert inherited["own"] == {"task_max_lines": 30}
    refused = client.patch(f"/api/nodes/{top}/settings", json={"report_detail": "huge"})
    assert refused.status_code == 422
    assert client.delete(f"/api/nodes/{top}/settings").json()["own"] == {}
    assert client.patch("/api/nodes/abcdef12/settings", json={"eager": True}).status_code == 404


def test_an_override_for_a_deleted_node_shows_as_a_problem(
    client: TestClient, project: Project
) -> None:
    config = project.apm_dir / "apm.yaml"
    config.write_text(
        config.read_text() + "features:\n  overrides:\n    abcdef12: { eager: true }\n"
    )
    warnings = client.get("/api/tree").json()["warnings"]
    assert {"code": "apm.yaml", "nodes": []}.items() <= warnings[0].items()
    assert "abcdef12 matches no node" in warnings[0]["message"]


def test_review_all_tasks_starts_what_fits_and_the_server_starts_the_rest(
    client: TestClient, project: Project
) -> None:
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + "runner:\n  max_concurrent: 1\n")
    top, tasks = feature(project, "Parse", "Index", "Rank")
    batch = client.post(f"/api/nodes/{top}/review-tasks").json()
    assert [run["node_id"] for run in batch["started"]] == [tasks[0]]
    assert [ref["id"] for ref in batch["waiting"]] == tasks[1:]
    assert batch["skipped"] == []
    runs = settled_runs(client, 3)
    assert sorted(r["node_id"] for r in runs) == sorted(tasks)
    assert {r["agent"] for r in runs} == {"apm-task-reviewer"}


def test_implement_all_says_what_it_skipped_and_why(client: TestClient, project: Project) -> None:
    top, (ready, unreviewed) = feature(project, "Parse", "Index")
    passed(project, ready)
    batch = client.post(f"/api/nodes/{top}/implement", json={"mode": "dontAsk"}).json()
    assert [(run["node_id"], run["mode"]) for run in batch["started"]] == [(ready, "dontAsk")]
    assert batch["waiting"] == []
    (skipped,) = batch["skipped"]
    assert skipped["id"] == unreviewed
    assert skipped["title"] == "Index"
    assert "has not been reviewed" in skipped["reason"]
    settled_runs(client, 1)
    refused = client.post(f"/api/nodes/{top}/implement", json={"mode": "yolo"})
    assert refused.status_code == 422
