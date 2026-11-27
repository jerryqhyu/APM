"""Task reviews start on their own whichever way a task is edited: the API, the CLI or the MCP.

Each test runs `apm serve`'s app, so the `AutoQueue` is driven by the server's own hub: the edit
lands as a plan commit, the head watcher announces it, and the queue scans and starts the review.
"""

import asyncio
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from apm.cli import app as cli
from apm.core.ids import handle
from apm.core.model import Status
from apm.core.ops import attach_delivery, set_status
from apm.core.project import Project
from apm.mcp.server import build_server
from apm.runner.auto import TASK_REVIEWER, AutoQueue
from apm.server import app as server
from tests.runner.fake import FakeClaude
from tests.runner.test_auto import tasks
from tests.runner.test_runner import Harness

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
QUIET_S = 2.0
"""Long enough for the head watcher and the queue to act on a commit several times over."""


@pytest.fixture
def client(
    project: Project, fake_claude: FakeClaude, reviewed: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The server's app, with its queue starting runs at once rather than after the debounce.

    It asks for `reviewed` so the task's review is recorded before the server starts: otherwise
    the queue's startup scan can see the task unreviewed and start a review of its own.
    """
    monkeypatch.setattr(server, "AutoQueue", partial(AutoQueue, debounce_s=0))
    app = server.create_app(project, TOKEN, static_dir=None)
    with TestClient(app, base_url="http://127.0.0.1:4100", headers=AUTH) as test_client:
        yield test_client


@pytest.fixture
def reviewed(project: Project) -> str:
    """A task whose current content already passed its review, so it needs none at startup."""
    (task,) = tasks(project, "Parse")
    Harness(project).review(task)
    return task


def reviews(client: TestClient) -> list[dict[str, Any]]:
    return [r for r in client.get("/api/runs").json() if r["agent"] == TASK_REVIEWER]


def wait_for_reviews(client: TestClient, count: int, timeout: float = 20.0) -> None:
    """Waits until `count` task reviews have started and none is still running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = reviews(client)
        if len(runs) >= count and all(r["status"] != "running" for r in runs):
            return
        time.sleep(0.05)
    raise AssertionError(f"expected {count} task reviews, got {len(reviews(client))}")


def assert_one_review_after(client: TestClient, task: str, edit: Callable[[], object]) -> None:
    time.sleep(QUIET_S)
    assert reviews(client) == []
    edit()
    wait_for_reviews(client, 1)
    time.sleep(QUIET_S)  # the review's own finish must not start another
    runs = reviews(client)
    assert [(r["node_id"], r["status"]) for r in runs] == [(task, "succeeded")]


def test_an_api_edit_starts_one_task_review(client: TestClient, reviewed: str) -> None:
    def edit() -> None:
        response = client.patch(f"/api/nodes/{reviewed}", json={"title": "Parse the input"})
        assert response.status_code == 200, response.text

    assert_one_review_after(client, reviewed, edit)


def test_a_cli_edit_starts_one_task_review(
    client: TestClient, project: Project, reviewed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project.code_dir)

    def edit() -> None:
        result = CliRunner().invoke(cli, ["node", "set", handle(reviewed), "--title", "Parse it"])
        assert result.exit_code == 0, result.output

    assert_one_review_after(client, reviewed, edit)


def test_an_mcp_edit_starts_one_task_review(
    client: TestClient, project: Project, reviewed: str
) -> None:
    mcp = build_server(project, env={})

    def edit() -> None:
        arguments = {"node": handle(reviewed), "title": "Parse it"}
        asyncio.run(mcp.call_tool("apm_update_node", arguments))

    assert_one_review_after(client, reviewed, edit)


def test_status_and_delivery_changes_start_no_review(
    client: TestClient, project: Project, reviewed: str
) -> None:
    # Back to To do at the end: only To do tasks are reviewed, so the check isn't vacuous.
    set_status(project, reviewed, Status.IN_PROGRESS)
    attach_delivery(project, reviewed, branch="apm/parse", commits=["abc1234"])
    set_status(project, reviewed, Status.TODO)
    time.sleep(QUIET_S)
    assert reviews(client) == []
    # The queue was listening all along: an edit to the content does start one.
    response = client.patch(f"/api/nodes/{reviewed}", json={"title": "Parse the input"})
    assert response.status_code == 200, response.text
    wait_for_reviews(client, 1)
    assert [r["node_id"] for r in reviews(client)] == [reviewed]
