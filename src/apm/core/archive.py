"""Project archives: a whole project saved as one `.apm.zip` file.

An archive holds:

- `manifest.json`: what wrote the archive and what is in it (see `export_archive`).
- The plan files as committed at the plan repo's HEAD: `apm.yaml`, `graph.ndjson`, `nodes/*.md`.
- `plan.bundle`: a git bundle of every ref in the plan repo, so its full history comes along.
- With runs only: `runs.ndjson` (one run per line, with all its events) and `proposals.ndjson`.

Only those files are written. Machine-specific state stays behind: the code repo's `.apm-link`,
and everything under `.apm/.cache/` (the server token, the write lock, the database itself).
"""

import json
import os
import secrets
import sys
import tempfile
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apm import __version__
from apm.core.config import parse_config
from apm.core.errors import ApmError
from apm.core.files import CONFIG_FILE, GRAPH_FILE, NODES_DIR
from apm.core.git import git, rev_parse
from apm.core.lock import CACHE_DIR
from apm.core.ndjson import parse_graph
from apm.core.project import Project
from apm.core.store import DB_FILE, SCHEMA_VERSION, Proposal, Run, RunEvent, Store

FORMAT = "apm-archive"
FORMAT_VERSION = 1
SUFFIX = ".apm.zip"

MANIFEST_FILE = "manifest.json"
BUNDLE_FILE = "plan.bundle"
RUNS_FILE = "runs.ndjson"
PROPOSALS_FILE = "proposals.ndjson"


class ArchiveError(ApmError, RuntimeError):
    pass


def export_archive(project: Project, dest: Path, *, with_runs: bool = False) -> dict[str, Any]:
    """Writes the project to `dest` as an `.apm.zip` and returns its manifest.

    The file appears all at once: it is written next to `dest` and renamed into place, so a failed
    export leaves no partial archive. With `with_runs`, run transcripts go in too, and they can hold
    tool output and secrets: saying so is the caller's job.
    """
    apm_dir = project.apm_dir
    if rev_parse(apm_dir, "HEAD") is None:
        raise ArchiveError(f"{apm_dir} has no commits to export")

    plan_files = _plan_files(apm_dir)
    config = parse_config(plan_files[CONFIG_FILE].decode(), CONFIG_FILE)
    nodes = parse_graph(plan_files[GRAPH_FILE].decode(), GRAPH_FILE)
    commits = int(git(apm_dir, ["rev-list", "--all", "--count"]).strip())

    manifest: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "apm_version": __version__,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": config.name,
        "counts": {"nodes": len(nodes), "commits": commits},
    }
    extra: dict[str, bytes] = {}
    if with_runs:
        runs, proposals = _read_store(apm_dir)
        manifest["store_schema_version"] = SCHEMA_VERSION
        manifest["counts"] |= {
            "runs": len(runs),
            "events": sum(len(events) for _, events in runs),
            "proposals": len(proposals),
        }
        extra[RUNS_FILE] = _ndjson(_run_record(run, events) for run, events in runs)
        extra[PROPOSALS_FILE] = _ndjson(asdict(proposal) for proposal in proposals)

    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="apm-export-") as scratch:
            bundle = Path(scratch) / BUNDLE_FILE
            git(apm_dir, ["bundle", "create", "--quiet", str(bundle), "--all"])
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(MANIFEST_FILE, json.dumps(manifest, indent=2) + "\n")
                for name, data in plan_files.items():
                    archive.writestr(name, data)
                archive.write(bundle, BUNDLE_FILE)
                for name, data in extra.items():
                    archive.writestr(name, data)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return manifest


def _plan_files(apm_dir: Path) -> dict[str, bytes]:
    """`apm.yaml`, `graph.ndjson` and every `nodes/*.md`, exactly as committed at HEAD.

    HEAD rather than the working tree, so the files agree with the bundle.
    """
    listing = git(apm_dir, ["ls-tree", "-r", "-z", "--name-only", "HEAD"])
    names = [
        name
        for name in listing.split("\0")
        if name in (CONFIG_FILE, GRAPH_FILE)
        or (name.startswith(f"{NODES_DIR}/") and name.endswith(".md") and name.count("/") == 1)
    ]
    for required in (CONFIG_FILE, GRAPH_FILE):
        if required not in names:
            raise ArchiveError(f"{apm_dir}: {required} is not committed at HEAD")
    return {name: git(apm_dir, ["show", f"HEAD:{name}"]).encode() for name in sorted(names)}


def _read_store(apm_dir: Path) -> tuple[list[tuple[Run, list[RunEvent]]], list[Proposal]]:
    """Every run (oldest first) with its events, and every proposal (oldest first)."""
    if not (apm_dir / CACHE_DIR / DB_FILE).exists():
        return [], []  # no runs ever: don't create a database just to read nothing from it
    store = Store(apm_dir)
    runs = list(reversed(store.list_runs(limit=sys.maxsize)))
    proposals = list(reversed(store.list_proposals()))
    return [(run, store.list_events(run.id)) for run in runs], proposals


def _run_record(run: Run, events: list[RunEvent]) -> dict[str, Any]:
    return {**asdict(run), "events": [asdict(event) for event in events]}


def _ndjson(records: Any) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode()
