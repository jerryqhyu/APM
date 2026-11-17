import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apm import __version__
from apm.cli import app, open_when_listening
from apm.core.git import git, log
from apm.core.project import Project, load_graph
from apm.server.security import load_or_create_token
from tests.runner.fake import FakeClaude

runner = CliRunner()


def apm(*args: str, code: int = 0) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == code, result.output
    return result.output


@pytest.fixture
def cwd(project: Project, monkeypatch: pytest.MonkeyPatch) -> Project:
    """Runs the test from inside a fresh project."""
    monkeypatch.chdir(project.code_dir)
    return project


def add(title: str, *args: str) -> str:
    """Adds a node and returns its handle."""
    return apm("node", "add", title, *args).split()[0]


def test_version_and_help() -> None:
    assert apm("--version").strip() == __version__
    assert apm("-v").strip() == __version__
    assert "node" in apm("--help")
    assert "Usage" in runner.invoke(app, []).output


def test_runs_as_a_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "apm", "--version"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == __version__


def test_init_then_build_a_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = tmp_path / "shop"
    code.mkdir()
    monkeypatch.chdir(code)
    assert "created" in apm("init")
    assert "no nodes yet" in apm("node", "ls")

    ship = add("Ship v1")
    build = add("Build", "--parent", ship, "--intent", "Make it work.")
    test = add("Test", "-p", ship, "--dep", build)
    choice = add("Pick a database", "--kind", "decision")

    assert apm("node", "ls").splitlines() == [
        f"{ship} [work/todo·ready] Ship v1 (0/2)",
        f"  {build} [work/todo·ready] Build",
        f"  {test} [work/todo·blocked] Test → {build}",
        f"{choice} [decision/todo·ready] Pick a database",
    ]
    assert apm("node", "ls", "--depth", "1").splitlines()[0].endswith("[+2 below]")
    assert apm("node", "ls", "--root", ship).splitlines() == [
        f"{build} [work/todo·ready] Build",
        f"{test} [work/todo·blocked] Test → {build}",
    ]
    assert apm("ready").splitlines() == [
        f"{build} [work/todo·ready] Build",
        f"{choice} [decision/todo·ready] Pick a database",
    ]
    assert apm("ready", "--root", ship).splitlines() == [f"{build} [work/todo·ready] Build"]

    apm("node", "status", build, "done", "--note", "Shipped.")
    assert apm("ready", "--root", ship).splitlines() == [f"{test} [work/todo·ready] Test → {build}"]
    assert apm("check").strip() == "ok"
    assert log(code / ".apm")[0].subject == f"status {build}: done"


def test_show_prints_links_and_body(cwd: Project) -> None:
    parent = add("Parent")
    first = add("First", "-p", parent, "--intent", "Do the thing.")
    second = add("Second", "-p", parent, "--dep", first)
    shown = apm("node", "show", second)
    assert shown.splitlines()[0] == f"{second} [work/todo·blocked] Second → {first}"
    assert f"parent: {parent} Parent" in shown
    assert f"depends on: {first} First" in shown
    assert f"waiting on: {first} First" in shown
    assert f"needed by: {second} Second" in apm("node", "show", first)
    assert "Do the thing." in apm("node", "show", first)

    data = json.loads(apm("node", "show", first, "--json"))
    assert (data["handle"], data["title"], data["ready"]) == (first, "First", True)
    assert data["body"].startswith("## Intent\n\nDo the thing.")


def test_json_output_for_lists(cwd: Project) -> None:
    parent = add("Parent")
    child = add("Child", "-p", parent)
    nodes = json.loads(apm("node", "ls", "--json"))
    assert [n["handle"] for n in nodes] == [parent, child]
    assert nodes[0]["progress"] == {"done": 0, "total": 1}
    assert [n["handle"] for n in json.loads(apm("node", "ls", "--root", parent, "--json"))] == [
        child
    ]
    assert [n["handle"] for n in json.loads(apm("ready", "--json"))] == [child]
    assert json.loads(apm("search", "child", "--json"))[0]["title"] == "Child"


def test_set_move_link_and_delete(cwd: Project) -> None:
    a = add("A")
    b = add("B")
    child = add("Child", "-p", a)

    assert apm("node", "set", a, "--title", "Alpha", "--kind", "decision").strip() == "updated"
    assert apm("node", "set", a, "--title", "Alpha").strip() == "nothing to change"
    assert "[decision/todo·ready] Alpha" in apm("node", "ls")

    apm("node", "mv", child, b)
    assert load_graph(cwd).children(None) != ()
    assert f"parent: {b} B" in apm("node", "show", child)
    apm("node", "mv", child, "--top")
    assert "parent:" not in apm("node", "show", child)
    assert "give a new parent, or --top" in apm("node", "mv", child, code=1)

    assert apm("link-dep", a, b).strip() == "linked"
    assert f"→ {b}" in apm("node", "show", a)
    assert "I2: dependency cycle" in apm("link-dep", b, a, code=1)
    assert apm("unlink-dep", a, b).strip() == "unlinked"

    apm("node", "mv", child, a)
    assert "has 1 child" in apm("node", "rm", a, code=1)
    assert apm("node", "rm", a, "--recursive").strip() == "deleted"
    assert [n.title for n in load_graph(cwd).nodes.values()] == ["B"]


def test_edit_uses_the_editor(
    cwd: Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    node = add("Node")
    editor = tmp_path / "editor.py"
    editor.write_text("import sys; open(sys.argv[1], 'a').write('Edited by hand.\\n')")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")
    monkeypatch.delenv("VISUAL", raising=False)
    assert apm("node", "edit", node).strip() == "saved"
    assert "Edited by hand." in apm("node", "show", node)
    assert log(cwd.apm_dir)[0].subject == f"edit-body {node}"

    monkeypatch.setenv("EDITOR", "definitely-not-an-editor")
    assert "editor failed" in apm("node", "edit", node, code=1)


def test_commit_takes_hand_edits(cwd: Project) -> None:
    node = add("Node")
    assert apm("commit").strip() == "nothing to change"
    graph_file = cwd.apm_dir / "graph.ndjson"
    graph_file.write_text(graph_file.read_text().replace('"Node"', '"Renamed"'))
    assert apm("commit").strip() == "committed 1 node(s)"
    assert f"{node} [work/todo·ready] Renamed" in apm("node", "ls")


def test_check_reports_problems_with_exit_code_1(cwd: Project) -> None:
    node = add("Node")
    graph_file = cwd.apm_dir / "graph.ndjson"
    line = json.loads(graph_file.read_text())
    graph_file.write_text(json.dumps({**line, "depends_on": [line["id"]]}) + "\n")
    assert apm("check", code=1).strip() == f"error I2: dependency cycle: {node} → {node}"


def test_errors_are_one_line_messages(cwd: Project, tmp_path: Path) -> None:
    assert "error: invalid handle" in apm("node", "show", "zz", code=1)
    assert "error: no node matches" in apm("node", "show", "abcd1234", code=1)
    os.chdir(tmp_path)
    assert "error: not in an APM project" in apm("node", "ls", code=1)


def test_search_and_plugin_path(cwd: Project) -> None:
    node = add("Stream output", "--intent", "Tail the log.")
    assert apm("search", "tail").strip() == f"{node} Stream output  — Tail the log."
    assert apm("search", "nothing").strip() == ""
    assert Path(apm("plugin-path").strip()).name == "plugin"


def test_link_push_clone_and_pull(cwd: Project, tmp_path: Path) -> None:
    remote = tmp_path / "plan.git"
    git(tmp_path, ["init", "--quiet", "--bare", "-b", "main", str(remote)])
    assert "plan repo remote" in apm("link", "--remote", str(remote))
    assert json.loads((cwd.code_dir / ".apm-link").read_text())["remote"] == str(remote)
    add("Shared node")
    apm("push")

    # A second checkout of the code: only .apm-link comes with it.
    other = tmp_path / "other"
    other.mkdir()
    (other / ".apm-link").write_text((cwd.code_dir / ".apm-link").read_text())
    os.chdir(other)
    assert "no such" not in apm("clone")
    assert "Shared node" in apm("node", "ls")
    assert "already exists" in apm("clone", code=1)

    os.chdir(cwd.code_dir)
    add("Second node")
    apm("push")
    os.chdir(other)
    apm("pull")
    assert "Second node" in apm("node", "ls")


# --- runs, proposals and the server -------------------------------------------------------------


def test_run_a_decomposer_then_accept_part_of_its_proposal(
    cwd: Project, fake_claude: FakeClaude
) -> None:
    node = add("File I/O")
    output = apm("run", "decomposer", node, "--guidance", "Keep it small.")
    assert "▶ apm-decomposer" in output
    assert "I am apm-decomposer" in output
    assert "▶ apm-critic" in output
    assert "— finished · $0.25" in output
    proposal = next(
        line.split()[1].rstrip(":") for line in output.splitlines() if line.startswith("proposal ")
    )
    assert f"next: apm proposal show {proposal}" in output
    assert "Keep it small." in fake_claude.calls[0]["argv"][1]

    assert apm("proposal", "ls").strip() == f"{proposal} [pending] 2 children for {node}"
    shown = apm("proposal", "show", proposal)
    assert "Parse first, then write." in shown
    assert "[a] Parse (work)" in shown
    assert "[b] Write (work) → a" in shown
    assert "- rejects bad lines" in shown
    assert "review: accept. Looks fine." in shown

    created = apm("proposal", "accept", proposal, "--only", "a")
    assert created.split()[1:] == ["Parse"]
    assert load_graph(cwd).children(next(iter(load_graph(cwd).children(None)))) != ()
    assert apm("proposal", "ls").strip() == "no proposals"
    assert "[accepted]" in apm("proposal", "ls", "--all")
    assert "already accepted" in apm("proposal", "accept", proposal, code=1)
    assert "no proposal nope" in apm("proposal", "show", "nope", code=1)


def test_reject_a_proposal_and_report_a_failed_run(cwd: Project, fake_claude: FakeClaude) -> None:
    node = add("File I/O")
    output = apm("run", "apm-decomposer", node, "--no-critic")
    proposal = next(
        line.split()[1].rstrip(":") for line in output.splitlines() if line.startswith("proposal ")
    )
    assert apm("proposal", "reject", proposal).strip() == "rejected"
    assert load_graph(cwd).is_leaf(next(iter(load_graph(cwd).nodes)))

    fake_claude.mode("fail")
    failed = apm("run", "assistant", node, code=1)
    assert "✗ failed: claude exited with code 1: boom: something broke" in failed

    parent = add("Parent")
    add("Child", "-p", parent)
    assert "only a leaf can be implemented" in apm("run", "implementer", parent, code=1)
    assert "unknown agent" in apm("run", "critic", node, code=1)


def test_serve_prints_a_url_that_works(cwd: Project) -> None:
    add("Served node")
    process = subprocess.Popen(
        [sys.executable, "-m", "apm", "serve", "--no-open", "--port", "0"],
        cwd=cwd.code_dir,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        url = process.stdout.readline().split()[-1]
        assert url.startswith("http://127.0.0.1:")
        base, token = url.split("/?token=")
        saved = json.loads((cwd.apm_dir / ".cache" / "server.json").read_text())
        assert (saved["token"], saved["pid"]) == (token, process.pid)
        assert f":{saved['port']}" in base

        def get(path: str, auth: str | None) -> tuple[int, bytes]:
            request = urllib.request.Request(base + path)
            if auth:
                request.add_header("Authorization", f"Bearer {auth}")
            for _ in range(100):
                try:
                    with urllib.request.urlopen(request) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as err:
                    return err.code, err.read()
                except urllib.error.URLError:
                    time.sleep(0.1)  # still starting
            raise AssertionError("the server never came up")

        status, body = get("/api/tree", token)
        assert status == 200
        assert [n["title"] for n in json.loads(body)["nodes"]] == ["Served node"]
        assert get("/api/tree", "wrong")[0] == 401
        assert get("/api/tree", None)[0] == 401
        assert get("/", None)[0] == 200
    finally:
        process.terminate()
        process.wait(timeout=10)

    # The token is kept, so a bookmarked URL still works after a restart.
    assert load_or_create_token(cwd.apm_dir) == token


def test_the_browser_opens_only_once_the_server_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        open_when_listening(port, "http://x", timeout=0.3)
        assert opened == []  # bound but not listening yet
        server.listen()
        open_when_listening(port, "http://x", timeout=2)
    assert opened == ["http://x"]
