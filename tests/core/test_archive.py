import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from apm import __version__
from apm.core import archive
from apm.core.archive import (
    BUNDLE_FILE,
    FORMAT_VERSION,
    MANIFEST_FILE,
    PROPOSALS_FILE,
    RUNS_FILE,
    ArchiveError,
    export_archive,
)
from apm.core.git import git
from apm.core.lock import LockTimeoutError, acquire_write_lock
from apm.core.ops import NewNode, create_nodes
from apm.core.project import LINK_FILE, Project
from apm.core.store import SCHEMA_VERSION, Store
from apm.server.security import write_server_file

RUN_FIELDS = {"store_schema_version"}
RUN_COUNTS = {"runs", "events", "proposals"}


@pytest.fixture
def plan(project: Project) -> Project:
    """A project with three nodes, each in its own commit."""
    parent = create_nodes(project, [NewNode(title="Parent")]).created[0]
    create_nodes(project, [NewNode(title="Child", parent=parent)])
    create_nodes(project, [NewNode(title="Ünïcode", body="## Intent\n\nnaïve café\n")])
    return project


def entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_the_archive_holds_the_plan_files_and_a_full_bundle(plan: Project, tmp_path: Path) -> None:
    dest = tmp_path / "out" / "plan.apm.zip"
    manifest = export_archive(plan, dest)

    files = entries(dest)
    node_files = sorted(p.name for p in (plan.apm_dir / "nodes").glob("*.md"))
    assert len(node_files) == 3
    assert set(files) == {
        MANIFEST_FILE,
        "apm.yaml",
        "graph.ndjson",
        BUNDLE_FILE,
        *(f"nodes/{name}" for name in node_files),
    }
    for name in ("apm.yaml", "graph.ndjson", *(f"nodes/{n}" for n in node_files)):
        assert files[name] == (plan.apm_dir / name).read_bytes()
    assert json.loads(files[MANIFEST_FILE]) == manifest
    assert not list(dest.parent.glob("*.tmp"))

    bundle = tmp_path / "check.bundle"
    bundle.write_bytes(files[BUNDLE_FILE])
    git(plan.apm_dir, ["bundle", "verify", "--quiet", str(bundle)])
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(bundle), str(clone)], check=True)
    source = git(plan.apm_dir, ["rev-list", "--all"]).split()
    assert git(clone, ["rev-list", "--all"]).split() == source
    assert len(source) == 4  # init + three creates


def test_the_manifest_without_runs(plan: Project, tmp_path: Path) -> None:
    Store(plan.apm_dir).create_run(node_id="", agent="a")  # present, but not asked for
    dest = tmp_path / "plan.apm.zip"
    manifest = export_archive(plan, dest)

    assert manifest["format_version"] == FORMAT_VERSION
    assert manifest["apm_version"] == __version__
    assert manifest["project"] == "my-app"
    assert manifest["counts"] == {"nodes": 3, "commits": 4}
    assert "exported_at" in manifest
    assert not RUN_FIELDS & manifest.keys()
    assert not RUN_COUNTS & manifest["counts"].keys()
    assert not {RUNS_FILE, PROPOSALS_FILE} & entries(dest).keys()


def test_with_runs_every_run_goes_in_with_its_events(plan: Project, tmp_path: Path) -> None:
    store = Store(plan.apm_dir)
    runs = [store.create_run(node_id="", agent=f"agent-{i}", prompt=f"p{i}") for i in range(105)]
    store.finish_run(runs[0].id, "succeeded", cost_usd=0.5)
    store.add_event(runs[0].id, {"type": "assistant", "text": "é"})
    store.add_event(runs[0].id, {"type": "result"})
    store.add_event(runs[-1].id, {"type": "assistant"})
    first = store.create_proposal(node_id="", data={"children": []}, run_id=runs[0].id)
    store.create_proposal(node_id="", data={"children": [1]}, review={"verdict": "ok"})
    store.resolve_proposal(first.id, "accepted")
    assert len(store.list_runs()) == 100  # list_runs' default limit, which export must not hit

    dest = tmp_path / "plan.apm.zip"
    manifest = export_archive(plan, dest, with_runs=True)

    assert manifest["store_schema_version"] == SCHEMA_VERSION
    assert manifest["counts"] == {
        "nodes": 3,
        "commits": 4,
        "runs": 105,
        "events": 3,
        "proposals": 2,
    }
    files = entries(dest)
    lines = [json.loads(line) for line in files[RUNS_FILE].decode().splitlines()]
    assert len(lines) == 105
    by_id = {line["id"]: line for line in lines}
    assert set(by_id) == {run.id for run in runs}
    done = store.get_run(runs[0].id)
    assert by_id[done.id] == {
        "id": done.id,
        "node_id": "",
        "agent": "agent-0",
        "session_id": done.session_id,
        "status": "succeeded",
        "started": done.started,
        "ended": done.ended,
        "cost_usd": 0.5,
        "error": None,
        "prompt": "p0",
        "parent_run": None,
        "pid": None,
        "mode": "",
        "events": [{"seq": e.seq, "ts": e.ts, "data": e.data} for e in store.list_events(done.id)],
    }
    assert [e["data"] for e in by_id[runs[-1].id]["events"]] == [{"type": "assistant"}]
    assert by_id[runs[1].id]["events"] == []

    proposals = [json.loads(line) for line in files[PROPOSALS_FILE].decode().splitlines()]
    assert {p["id"]: p["status"] for p in proposals} == {
        p.id: p.status for p in store.list_proposals()
    }
    assert next(p for p in proposals if p["id"] == first.id)["run_id"] == runs[0].id


def test_with_runs_and_no_database_yet(plan: Project, tmp_path: Path) -> None:
    db = plan.apm_dir / ".cache" / "apm.db"
    assert not db.exists()
    manifest = export_archive(plan, tmp_path / "plan.apm.zip", with_runs=True)
    assert manifest["counts"] == {
        "nodes": 3,
        "commits": 4,
        "runs": 0,
        "events": 0,
        "proposals": 0,
    }
    files = entries(tmp_path / "plan.apm.zip")
    assert files[RUNS_FILE] == files[PROPOSALS_FILE] == b""
    assert not db.exists()


def test_the_link_and_the_server_token_stay_behind(plan: Project, tmp_path: Path) -> None:
    token = "secret-token-" + "x" * 40
    write_server_file(plan.apm_dir, token=token, port=8765)
    link = (plan.code_dir / LINK_FILE).read_bytes()
    Store(plan.apm_dir).create_run(node_id="", agent="a")

    for with_runs in (False, True):
        dest = tmp_path / f"plan-{with_runs}.apm.zip"
        export_archive(plan, dest, with_runs=with_runs)
        files = entries(dest)
        assert not any(LINK_FILE in name or ".cache" in name for name in files)
        raw = dest.read_bytes()
        for data in (raw, *files.values()):
            assert token.encode() not in data
            assert link not in data


def test_a_failed_export_leaves_nothing_behind(
    plan: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "write", broken)
    with pytest.raises(OSError, match="disk full"):
        export_archive(plan, tmp_path / "plan.apm.zip")
    assert list(tmp_path.iterdir()) == [plan.code_dir]


def test_an_uncommitted_plan_is_refused(tmp_path: Path) -> None:
    apm_dir = tmp_path / ".apm"
    apm_dir.mkdir()
    git(apm_dir, ["init", "--quiet", "-b", "main"])
    with pytest.raises(ArchiveError, match="no commits"):
        export_archive(Project(tmp_path, apm_dir), tmp_path / "plan.apm.zip")


def test_export_holds_the_write_lock_so_a_change_cannot_land_halfway(
    plan: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = archive._plan_files
    blocked: list[bool] = []

    def plan_files_then_try_to_write(apm_dir: Path) -> dict[str, bytes]:
        try:
            acquire_write_lock(apm_dir, timeout=0.05)
        except LockTimeoutError:
            blocked.append(True)
        return real(apm_dir)

    monkeypatch.setattr(archive, "_plan_files", plan_files_then_try_to_write)
    export_archive(plan, tmp_path / "out.apm.zip")
    assert blocked == [True]
    acquire_write_lock(plan.apm_dir, timeout=0.05).release()  # free again afterwards
