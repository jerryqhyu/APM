"""The feature report over the API: where it stands, and Write report."""

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apm.core.git import git
from apm.core.ids import handle
from apm.core.project import Project
from apm.server.app import create_app
from tests.conftest import CodeRepo
from tests.core.test_feature_review import built, feature
from tests.fake_gh import FakeGh
from tests.runner.fake import FakeClaude
from tests.server.test_app import AUTH, BASE, TOKEN


@pytest.fixture
def client(code_repo: CodeRepo, fake_claude: FakeClaude) -> Iterator[TestClient]:
    project = code_repo.project
    # Only what a test starts runs: nothing on its own in between.
    off = "automation:\n  task_review: false\n  feature_review: false\n  reconcile: false\n"
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + off)
    app = create_app(project, TOKEN, static_dir=None)
    with TestClient(app, base_url=BASE, headers=AUTH) as test_client:
        yield test_client


def settled(client: TestClient, node: str, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/nodes/{node}/report").json()
        if status["state"] != "writing" and status["report"] is not None:
            return status
        time.sleep(0.05)
    raise AssertionError(client.get(f"/api/nodes/{node}/report").json())


def test_write_report_and_read_it_back(
    client: TestClient, code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    project: Project = code_repo.project
    node, step, _, branch = built(code_repo, project, "Write")

    status = client.get(f"/api/nodes/{handle(step)}/report").json()  # a step: its feature's
    assert status["feature"] == node
    assert (status["state"], status["report"], status["url"]) == ("none", None, None)
    assert (status["automatic"], status["detail"]) == (True, "standard")

    started = client.post(f"/api/nodes/{handle(node)}/report", json={"message": "Be brief."})
    assert started.status_code == 201
    run = started.json()
    assert (run["agent"], run["node_id"]) == ("apm-feature-reporter", node)

    status = settled(client, node)
    assert status["state"] == "written"
    report = status["report"]
    path = f"docs/features/export-{handle(node)}.md"
    assert report["sha"] == git(code_repo.dir, ["rev-parse", f"origin/{branch}"]).strip()
    assert report["path"] == path
    assert report["markdown"].startswith("# Search\n\nA standard report")
    assert status["url"] == f"https://github.com/o/r/blob/{branch}/{path}"
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "succeeded"


def test_write_report_is_refused_on_what_has_no_report(
    client: TestClient, code_repo: CodeRepo
) -> None:
    node, _, (task,) = feature(code_repo.project, "Write")
    refused = client.post(f"/api/nodes/{handle(task)}/report")
    assert refused.status_code == 422
    assert "only a feature has a report" in refused.json()["error"]
    refused = client.post(f"/api/nodes/{handle(node)}/report")
    assert "nothing is built yet" in refused.json()["error"]
    assert client.get("/api/nodes/ffffffff/report").status_code == 404
