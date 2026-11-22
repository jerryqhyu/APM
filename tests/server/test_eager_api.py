"""Eager mode over the API and in the terminal: the feature's eager build in its details, a task's
place in it, Redo, and `apm eager show` / `apm eager redo`.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from apm.cli import app as cli
from apm.core.git import git
from apm.core.ids import handle
from apm.core.model import NodeId
from apm.core.store import EagerSlice, Store
from apm.server.app import create_app
from tests.conftest import CodeRepo
from tests.core.test_eager import EAGER_ON, chain, configure, feature_of_three
from tests.runner.fake import FakeClaude
from tests.server.test_app import AUTH, BASE, TOKEN

__all__ = ["chain"]  # a fixture, used by name

type Chain = tuple[CodeRepo, Store, NodeId, list[EagerSlice], str]


@pytest.fixture
def client(code_repo: CodeRepo, fake_claude: FakeClaude) -> Iterator[TestClient]:
    configure(code_repo.project, "automation:\n  task_review: false\n  reconcile: false\n")
    app = create_app(code_repo.project, TOKEN, static_dir=None)
    with TestClient(app, base_url=BASE, headers=AUTH) as test_client:
        yield test_client


def test_a_feature_with_eager_on_shows_it_before_anything_is_built(
    code_repo: CodeRepo, client: TestClient
) -> None:
    feature, tasks = feature_of_three(code_repo.project)
    assert client.get(f"/api/nodes/{feature}").json()["eager"] is None  # eager is off
    configure(code_repo.project, EAGER_ON)
    view = client.get(f"/api/nodes/{feature}").json()["eager"]
    assert (view["applies"], view["build"], view["tasks"]) == (True, None, [])
    assert view["redo"]["action"] is None
    task = client.get(f"/api/nodes/{tasks[0]}").json()["eager"]
    assert (task["feature"]["id"], task["in_split"], task["redirect"]) == (feature, False, True)
    # Implement on a task goes to the feature's build, which the gate holds back.
    response = client.post("/api/runs", json={"agent": "apm-implementer", "node_id": tasks[0]})
    assert response.status_code == 422
    assert "can't start until every task in it is ready" in response.json()["error"]


def test_a_split_feature_shows_its_chain_and_redo(client: TestClient, chain: Chain) -> None:
    repo, _, feature, slices, home = chain
    view = client.get(f"/api/nodes/{feature}").json()["eager"]
    assert view["build"]["state"] == "split"
    assert view["build"]["base"] == home
    assert [(t["id"], t["branch"], t["state"]) for t in view["tasks"]] == [
        (s.node_id, s.branch, "open") for s in slices
    ]
    task = client.get(f"/api/nodes/{slices[0].node_id}").json()["eager"]
    assert task["in_split"] is True
    assert "use Redo" in task["blocker"]

    response = client.post(f"/api/nodes/{feature}/eager/redo", json={})
    assert response.status_code == 422
    assert "nothing to redo" in response.json()["error"]

    # A review fix on the first task: Redo rebuilds the two after it.
    first = slices[0]
    git(repo.dir, ["checkout", "--quiet", "-B", "fix", first.commit])
    (repo.dir / "fix.py").write_text("fixed\n")
    git(repo.dir, ["add", "fix.py"])
    git(repo.dir, ["commit", "--quiet", "-m", "Review fix"])
    git(repo.dir, ["push", "--quiet", "origin", f"HEAD:refs/heads/{first.branch}"])
    git(repo.dir, ["checkout", "--quiet", "main"])
    response = client.post(f"/api/nodes/{feature}/eager/redo", json={"message": "Keep it short."})
    assert response.status_code == 201, response.text
    run = response.json()
    assert (run["agent"], run["node_id"]) == ("apm-eager-builder", feature)


def test_apm_eager_show_and_redo_in_the_terminal(
    chain: Chain, fake_claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, feature, slices, _ = chain
    monkeypatch.chdir(repo.dir)
    result = CliRunner().invoke(cli, ["eager", "show", handle(feature)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "eager build 1 on eager/x, split" in lines[0]
    assert [line.split()[1] for line in lines[1:4]] == [handle(s.node_id) for s in slices]
    result = CliRunner().invoke(cli, ["eager", "redo", handle(feature)])
    assert result.exit_code == 1
    assert "nothing to redo" in result.output


def test_apm_eager_show_says_when_eager_mode_is_off(
    code_repo: CodeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature, _ = feature_of_three(code_repo.project)
    monkeypatch.chdir(code_repo.dir)
    result = CliRunner().invoke(cli, ["eager", "show", handle(feature)])
    assert result.exit_code == 0
    assert "eager mode is off" in result.output
