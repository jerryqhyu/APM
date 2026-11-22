import json
import socket
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apm.core.archive import (
    BUNDLE_FILE,
    FORMAT_VERSION,
    MANIFEST_FILE,
    RUNS_FILE,
    ArchiveError,
    export_archive,
    import_archive,
)
from apm.core.git import git
from apm.core.lifting import check_all
from apm.core.ops import NewNode, create_nodes
from apm.core.project import LINK_FILE, Project, load_graph, open_project
from apm.core.store import SCHEMA_VERSION, Store, TaskReview
from apm.core.views import outline
from apm.runner import runner


@pytest.fixture
def plan(project: Project) -> Project:
    """A project with three nodes, each in its own commit, and a second branch."""
    parent = create_nodes(project, [NewNode(title="Parent")]).created[0]
    create_nodes(project, [NewNode(title="Child", parent=parent)])
    create_nodes(project, [NewNode(title="Ünïcode", body="## Intent\n\nnaïve café\n")])
    git(project.apm_dir, ["branch", "side", "HEAD~1"])
    return project


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """An empty folder to import into."""
    folder = tmp_path / "restored"
    folder.mkdir()
    return folder


@pytest.fixture
def no_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails the test if anything starts, resumes or sets up a run."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("import must not touch the runner")

    for name in ("__init__", "start", "continue_run"):
        monkeypatch.setattr(runner.Runner, name, refuse)


def snapshot(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def rewrite(path: Path, change: Callable[[dict[str, bytes]], None]) -> None:
    """Rewrites the archive's entries through `change`."""
    with zipfile.ZipFile(path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    change(files)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)


def edit_manifest(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    def apply(files: dict[str, bytes]) -> None:
        manifest = json.loads(files[MANIFEST_FILE])
        change(manifest)
        files[MANIFEST_FILE] = json.dumps(manifest).encode()

    rewrite(path, apply)


def test_round_trip(plan: Project, target: Path, tmp_path: Path, no_runner: None) -> None:
    archive = tmp_path / "plan.apm.zip"
    manifest = export_archive(plan, archive)

    imported = import_archive(archive, target)

    assert imported.manifest == manifest
    restored = open_project(target)
    assert restored == imported.project
    assert restored.apm_dir == target / ".apm"
    for revisions in (["log", "--format=%H"], ["rev-list", "--all", "--date-order"]):
        assert git(restored.apm_dir, revisions) == git(plan.apm_dir, revisions)
    assert git(restored.apm_dir, ["branch", "--show-current"]) == "main\n"
    assert git(restored.apm_dir, ["for-each-ref"]) == git(plan.apm_dir, ["for-each-ref"])
    assert git(restored.apm_dir, ["status", "--porcelain"]) == ""
    assert git(restored.apm_dir, ["remote"]) == ""

    report = check_all(load_graph(restored))
    assert not report.errors
    assert not report.warnings
    assert outline(load_graph(restored)) == outline(load_graph(plan))
    assert (restored.apm_dir / "apm.yaml").read_bytes() == (plan.apm_dir / "apm.yaml").read_bytes()
    source_files = {k: v for k, v in snapshot(plan.apm_dir).items() if not k.startswith(".git")}
    assert {k: v for k, v in snapshot(restored.apm_dir).items() if not k.startswith(".git")} == (
        source_files
    )
    assert ".apm/" in (target / ".gitignore").read_text()
    # No runs in the archive: no database either.
    assert not (restored.apm_dir / ".cache").exists()
    assert not list(target.glob("*.import"))


def test_runs_events_and_proposals_come_back(
    plan: Project, target: Path, tmp_path: Path, no_runner: None
) -> None:
    store = Store(plan.apm_dir)
    done = store.create_run(node_id="", agent="apm-decomposer", prompt="p", mode="plan")
    store.add_event(done.id, {"type": "assistant", "text": "é"})
    store.add_event(done.id, {"type": "result"})
    done = store.finish_run(done.id, "succeeded", cost_usd=0.5)
    live = store.create_run(node_id="", agent="apm-critic", parent_run=done.id, pid=4242)
    store.add_event(live.id, {"type": "assistant"})
    pending = store.create_proposal(node_id="", data={"children": []}, run_id=done.id)
    accepted = store.create_proposal(node_id="", data={"children": [1]}, review={"v": "ok"})
    accepted = store.resolve_proposal(accepted.id, "accepted")
    store.add_task_review(TaskReview("n", "f", "pass", {}))
    archive = tmp_path / "plan.apm.zip"
    export_archive(plan, archive, with_runs=True)

    project = import_archive(archive, target).project

    restored = Store(project.apm_dir)
    # Task reviews are deliberately left behind: the imported tasks get reviewed again.
    assert restored.latest_task_review("n") is None
    assert restored.get_run(done.id) == done
    assert restored.get_run(live.id) == replace(live, status="cancelled", pid=None)
    assert [r.id for r in restored.list_runs()] == [r.id for r in store.list_runs()]
    for run in (done, live):
        assert restored.list_events(run.id) == store.list_events(run.id)
    assert restored.get_proposal(pending.id) == pending
    assert restored.get_proposal(accepted.id) == accepted
    assert restored.list_runs(status="running") == []
    # The source still has its running run: import only changes the copy.
    assert store.get_run(live.id).status == "running"


def test_runs_from_an_older_store_schema(plan: Project, target: Path, tmp_path: Path) -> None:
    store = Store(plan.apm_dir)
    run = store.finish_run(store.create_run(node_id="", agent="a").id, "failed", error="x")
    store.add_event(run.id, {"type": "result"})
    archive = tmp_path / "plan.apm.zip"
    export_archive(plan, archive, with_runs=True)

    def to_schema_1(files: dict[str, bytes]) -> None:
        manifest = json.loads(files[MANIFEST_FILE])
        manifest["store_schema_version"] = 1
        files[MANIFEST_FILE] = json.dumps(manifest).encode()
        records = [json.loads(line) for line in files[RUNS_FILE].splitlines()]
        for record in records:
            del record["mode"]  # schema 1 had no mode column
        files[RUNS_FILE] = "".join(json.dumps(r) + "\n" for r in records).encode()

    rewrite(archive, to_schema_1)
    project = import_archive(archive, target).project

    restored = Store(project.apm_dir)
    assert restored.get_run(run.id) == run
    assert restored.get_run(run.id).mode == ""
    assert [e.data for e in restored.list_events(run.id)] == [{"type": "result"}]


def test_a_newer_store_schema_is_refused_whole(plan: Project, target: Path, tmp_path: Path) -> None:
    Store(plan.apm_dir).create_run(node_id="", agent="a")
    archive = tmp_path / "plan.apm.zip"
    export_archive(plan, archive, with_runs=True)
    newer = SCHEMA_VERSION + 1
    edit_manifest(archive, lambda m: m.update(store_schema_version=newer))

    with pytest.raises(ArchiveError) as caught:
        import_archive(archive, target)
    message = str(caught.value)
    assert f"store schema version {newer}" in message
    assert f"up to version {SCHEMA_VERSION}" in message
    assert list(target.iterdir()) == []


def _newer_format(path: Path) -> None:
    edit_manifest(path, lambda m: m.update(format_version=FORMAT_VERSION + 1))


def _truncated(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])


def _corrupted_entry(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("graph.ndjson")
    data = bytearray(path.read_bytes())
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    data[start + info.compress_size // 2] ^= 0xFF
    path.write_bytes(bytes(data))


def _invalid_bundle(path: Path) -> None:
    rewrite(path, lambda files: files.update({BUNDLE_FILE: b"# v2 git bundle\nnot really\n"}))


def _counts(key: str) -> Callable[[Path], None]:
    def change(path: Path) -> None:
        edit_manifest(path, lambda m: m["counts"].update({key: m["counts"][key] + 1}))

    return change


def _a_run_too_many(path: Path) -> None:
    def add(files: dict[str, bytes]) -> None:
        line = files[RUNS_FILE].splitlines()[0]
        record = json.loads(line) | {"id": "another", "events": []}
        files[RUNS_FILE] += json.dumps(record).encode() + b"\n"

    rewrite(path, add)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (_newer_format, f"format version {FORMAT_VERSION + 1}.*up to version {FORMAT_VERSION}"),
        (_truncated, "truncated or corrupted"),
        (_corrupted_entry, "truncated or corrupted"),
        (_invalid_bundle, "not a valid git bundle"),
        (_counts("nodes"), "counts 5 nodes, but the archive holds 4"),
        (_counts("commits"), "counts 5 commits, but the archive holds 4"),
        (_counts("runs"), "counts 2 runs, but the archive holds 1"),
        (_counts("events"), "counts 2 events, but the archive holds 1"),
        (_counts("proposals"), "counts 2 proposals, but the archive holds 1"),
        (_a_run_too_many, "counts 1 runs, but the archive holds 2"),
    ],
    ids=[
        "newer-format",
        "truncated",
        "corrupted-entry",
        "invalid-bundle",
        "nodes-count",
        "commits-count",
        "runs-count",
        "events-count",
        "proposals-count",
        "extra-run",
    ],
)
def test_a_bad_archive_is_refused_and_leaves_nothing(
    plan: Project, target: Path, tmp_path: Path, damage: Callable[[Path], None], message: str
) -> None:
    store = Store(plan.apm_dir)
    run = store.create_run(node_id="", agent="a")
    store.add_event(run.id, {"type": "assistant"})
    store.create_proposal(node_id="", data={})
    archive = tmp_path / "plan.apm.zip"
    export_archive(plan, archive, with_runs=True)
    damage(archive)

    with pytest.raises(ArchiveError, match=message):
        import_archive(archive, target)
    assert list(target.iterdir()) == []


def test_a_folder_with_a_plan_is_left_alone(plan: Project, tmp_path: Path) -> None:
    archive = tmp_path / "plan.apm.zip"
    export_archive(plan, archive)
    Store(plan.apm_dir).create_run(node_id="", agent="a")
    before = snapshot(plan.code_dir)

    with pytest.raises(ArchiveError, match=str(plan.code_dir / LINK_FILE)):
        import_archive(archive, plan.code_dir)
    assert snapshot(plan.code_dir) == before

    # A bare .apm/ with no link counts as a plan too.
    other = tmp_path / "other"
    (other / ".apm").mkdir(parents=True)
    (other / ".apm" / "notes").write_text("mine")
    with pytest.raises(ArchiveError, match=str(other / ".apm")):
        import_archive(archive, other)
    assert snapshot(other) == {".apm/notes": b"mine"}


def test_import_makes_no_network_calls(
    plan: Project, target: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(plan.apm_dir, ["remote", "add", "origin", "https://example.invalid/plan.git"])
    Store(plan.apm_dir).create_run(node_id="", agent="a")
    archive = tmp_path / "plan.apm.zip"
    export_archive(plan, archive, with_runs=True)

    def offline(*args: object, **kwargs: object) -> None:
        raise AssertionError("import tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket, "create_connection", offline)
    monkeypatch.setattr(socket, "getaddrinfo", offline)
    # Git child processes can't reach a proxy or resolve a host either.
    for variable in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(variable, "http://127.0.0.1:9")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

    project = import_archive(archive, target).project
    assert git(project.apm_dir, ["remote"]) == ""


def test_export_import_export_gives_the_same_manifest(
    plan: Project, target: Path, tmp_path: Path
) -> None:
    store = Store(plan.apm_dir)
    store.add_event(store.create_run(node_id="", agent="a").id, {"type": "x"})
    store.create_proposal(node_id="", data={})
    first = export_archive(plan, tmp_path / "first.apm.zip", with_runs=True)
    project = import_archive(tmp_path / "first.apm.zip", target).project
    second = export_archive(project, tmp_path / "second.apm.zip", with_runs=True)

    del first["exported_at"], second["exported_at"]
    assert second == first
