"""Project archives: a whole project saved as one `.apm.zip` file.

An archive holds:

- `manifest.json`: what wrote the archive and what is in it (see `export_archive`).
- The plan files as committed at the plan repo's HEAD: `apm.yaml`, `graph.ndjson`, `nodes/*.md`.
- `plan.bundle`: a git bundle of every ref in the plan repo, so its full history comes along.
- With runs only: `runs.ndjson` (one run per line, with all its events) and `proposals.ndjson`.

Only those files are written. Machine-specific state stays behind: the code repo's `.apm-link`,
and everything under `.apm/.cache/` (the server token, the write lock, the database itself).

`import_archive` restores such a file into a folder with no plan, all or nothing.
"""

import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import zipfile
import zlib
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apm import __version__
from apm.core.config import parse_config
from apm.core.errors import ApmError
from apm.core.files import CONFIG_FILE, GRAPH_FILE, NODES_DIR
from apm.core.git import GitError, git, rev_parse
from apm.core.lock import CACHE_DIR
from apm.core.ndjson import parse_graph
from apm.core.project import DEFAULT_APM_DIR, LINK_FILE, Project, link_plan
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


# --- import ---


@dataclass(frozen=True, slots=True)
class Imported:
    project: Project
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Contents:
    """An archive, read and checked as far as it can be without restoring it."""

    manifest: dict[str, Any]
    plan_files: dict[str, bytes]
    bundle: bytes
    runs: list[tuple[Run, list[RunEvent]]] | None
    """None when the archive was written without runs."""
    proposals: list[Proposal] | None


def import_archive(src: Path, code_dir: Path) -> Imported:
    """Restores the project in the archive at `src` as `<code_dir>/.apm` and links it from
    `code_dir`, the way `apm init` does.

    All or nothing: the archive is checked in full (versions, every entry, the bundle, the counts)
    and the plan repo and its database are built in a temporary folder that only becomes `.apm/`
    once everything is in place. `code_dir` must not have a plan already. Imported runs are history:
    one that was running at export is stored as cancelled, and nothing is started or resumed.
    Import reads only `src` and never contacts a remote.
    """
    code_dir = code_dir.resolve()
    apm_dir = code_dir / DEFAULT_APM_DIR
    _check_no_plan(code_dir)
    contents = _read_archive(src)

    tmp = code_dir / f".apm.{os.getpid()}.{secrets.token_hex(4)}.import"
    try:
        _restore_repo(src, tmp, contents)
        if contents.runs is not None and contents.proposals is not None:
            _restore_store(src, tmp, contents.runs, contents.proposals)
        _check_no_plan(code_dir)  # a plan that appeared meanwhile is not ours to replace
        tmp.rename(apm_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        project = link_plan(code_dir)
    except BaseException:
        shutil.rmtree(apm_dir, ignore_errors=True)
        (code_dir / LINK_FILE).unlink(missing_ok=True)
        raise
    return Imported(project, contents.manifest)


def _check_no_plan(code_dir: Path) -> None:
    if not code_dir.is_dir():
        raise ArchiveError(f"{code_dir} is not a folder")
    for existing in (code_dir / LINK_FILE, code_dir / DEFAULT_APM_DIR):
        if existing.exists() or existing.is_symlink():
            raise ArchiveError(
                f"{existing} already exists: import only into a folder that has no plan"
            )


def _read_archive(src: Path) -> _Contents:
    try:
        with zipfile.ZipFile(src) as archive:
            names = set(archive.namelist())
            if MANIFEST_FILE not in names:
                raise ArchiveError(f"{src}: no {MANIFEST_FILE}, so not an APM archive")
            manifest = _check_manifest(src, archive.read(MANIFEST_FILE))
            with_runs = "store_schema_version" in manifest
            expected = {CONFIG_FILE, GRAPH_FILE, BUNDLE_FILE}
            if with_runs:
                expected |= {RUNS_FILE, PROPOSALS_FILE}
            if missing := sorted(expected - names):
                raise ArchiveError(f"{src}: missing {', '.join(missing)}")
            if not with_runs and (stray := sorted({RUNS_FILE, PROPOSALS_FILE} & names)):
                raise ArchiveError(
                    f"{src}: has {', '.join(stray)} but its manifest has no store schema version"
                )
            plan_files = {
                name: archive.read(name)
                for name in sorted(names)
                if name in (CONFIG_FILE, GRAPH_FILE)
                or (name.startswith(f"{NODES_DIR}/") and name.endswith(".md"))
            }
            bundle = archive.read(BUNDLE_FILE)
            runs = proposals = None
            if with_runs:
                runs = _parse_runs(src, archive.read(RUNS_FILE))
                proposals = _parse_proposals(src, archive.read(PROPOSALS_FILE))
    except FileNotFoundError:
        raise ArchiveError(f"{src}: no such file") from None
    except (zipfile.BadZipFile, zlib.error, EOFError, UnicodeDecodeError) as err:
        raise ArchiveError(f"{src} is truncated or corrupted: {err}") from None
    except OSError as err:
        raise ArchiveError(f"{src}: cannot read it: {err}") from None

    try:
        parse_config(plan_files[CONFIG_FILE].decode(), CONFIG_FILE)
        nodes = parse_graph(plan_files[GRAPH_FILE].decode(), GRAPH_FILE)
    except (ApmError, UnicodeDecodeError) as err:
        raise ArchiveError(f"{src}: {err}") from None

    actual = {"nodes": len(nodes)}
    if runs is not None and proposals is not None:
        actual |= {
            "runs": len(runs),
            "events": sum(len(events) for _, events in runs),
            "proposals": len(proposals),
        }
    for key, value in actual.items():
        _check_count(src, manifest["counts"], key, value)
    return _Contents(manifest, plan_files, bundle, runs, proposals)


def _check_manifest(src: Path, data: bytes) -> dict[str, Any]:
    """The manifest, if this APM can read the archive it describes. An archive whose format, or
    whose runs' store schema, is newer than this APM knows is refused before anything is written.
    """
    try:
        manifest = json.loads(data)
    except ValueError as err:
        raise ArchiveError(f"{src}: {MANIFEST_FILE} is not valid JSON: {err}") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ArchiveError(f"{src}: {MANIFEST_FILE} does not describe an APM archive")
    version = manifest.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ArchiveError(f"{src}: {MANIFEST_FILE} has no valid format_version")
    if version > FORMAT_VERSION:
        raise ArchiveError(
            f"{src} uses archive format version {version}, but this APM ({__version__}) reads"
            f" up to version {FORMAT_VERSION}: upgrade APM to import it"
        )
    if "store_schema_version" in manifest:
        schema = manifest["store_schema_version"]
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise ArchiveError(f"{src}: {MANIFEST_FILE} has an invalid store_schema_version")
        if schema > SCHEMA_VERSION:
            raise ArchiveError(
                f"{src} holds runs from store schema version {schema}, but this APM"
                f" ({__version__}) supports up to version {SCHEMA_VERSION}: upgrade APM to"
                " import it (nothing was imported)"
            )
    if not isinstance(manifest.get("counts"), dict):
        raise ArchiveError(f"{src}: {MANIFEST_FILE} has no counts")
    return manifest


def _check_count(src: Path, counts: dict[str, Any], key: str, actual: int) -> None:
    if counts.get(key) != actual:
        raise ArchiveError(
            f"{src}: the manifest counts {counts.get(key)} {key}, but the archive holds {actual}"
        )


_RUN_FIELDS = {f.name for f in fields(Run)}
_RUN_STATUSES = ("running", "succeeded", "failed", "cancelled")
_PROPOSAL_STATUSES = ("pending", "accepted", "rejected")


def _parse_runs(src: Path, data: bytes) -> list[tuple[Run, list[RunEvent]]]:
    """Runs with their events. Fields a run from an older store schema lacks (such as `mode`)
    take their defaults. A run that was running at export is stored as cancelled: it can't be
    running here, and nothing would ever finish it.
    """
    runs = []
    for number, record in _records(src, RUNS_FILE, data):
        try:
            events = [RunEvent(**event) for event in record.pop("events", [])]
            if unknown := sorted(record.keys() - _RUN_FIELDS):
                raise TypeError(f"unknown fields {unknown}")
            run = Run(**record)
        except TypeError as err:
            raise ArchiveError(f"{src}: {RUNS_FILE} line {number}: {err}") from None
        if run.status not in _RUN_STATUSES:
            raise ArchiveError(f"{src}: {RUNS_FILE} line {number}: unknown status {run.status!r}")
        if run.status == "running":
            run = replace(run, status="cancelled", pid=None)
        runs.append((run, events))
    return runs


def _parse_proposals(src: Path, data: bytes) -> list[Proposal]:
    proposals = []
    for number, record in _records(src, PROPOSALS_FILE, data):
        try:
            proposal = Proposal(**record)
        except TypeError as err:
            raise ArchiveError(f"{src}: {PROPOSALS_FILE} line {number}: {err}") from None
        if proposal.status not in _PROPOSAL_STATUSES:
            raise ArchiveError(
                f"{src}: {PROPOSALS_FILE} line {number}: unknown status {proposal.status!r}"
            )
        proposals.append(proposal)
    return proposals


def _records(src: Path, name: str, data: bytes) -> list[tuple[int, dict[str, Any]]]:
    records = []
    for number, line in enumerate(data.decode().splitlines(), 1):
        try:
            record = json.loads(line)
        except ValueError as err:
            raise ArchiveError(f"{src}: {name} line {number}: invalid JSON: {err}") from None
        if not isinstance(record, dict):
            raise ArchiveError(f"{src}: {name} line {number}: expected a JSON object")
        records.append((number, record))
    return records


def _restore_repo(src: Path, apm_dir: Path, contents: _Contents) -> None:
    """The plan repo at `apm_dir`, from the bundle: every ref, HEAD on the branch it was on, and
    the working tree checked out. Checks it agrees with the manifest and the plan files.
    """
    apm_dir.mkdir()
    git(apm_dir, ["init", "--quiet", "-b", "main"])
    with tempfile.TemporaryDirectory(prefix="apm-import-") as scratch:
        bundle = Path(scratch) / BUNDLE_FILE
        bundle.write_bytes(contents.bundle)
        try:
            git(apm_dir, ["bundle", "verify", "--quiet", str(bundle)])
            heads = git(apm_dir, ["bundle", "list-heads", str(bundle)])
            # From the local file only. --update-head-ok: main is the (unborn) current branch.
            git(apm_dir, ["fetch", "--quiet", "--update-head-ok", str(bundle), "+refs/*:refs/*"])
        except GitError as err:
            raise ArchiveError(
                f"{src}: {BUNDLE_FILE} is not a valid git bundle: {err.stderr.strip()}"
            ) from None

    refs: dict[str, str] = {}
    for line in heads.splitlines():
        sha, _, ref = line.partition(" ")
        refs[ref] = sha
    head = refs.get("HEAD")
    if head is None:
        raise ArchiveError(f"{src}: {BUNDLE_FILE} has no HEAD")
    branches = sorted(
        ref for ref, sha in refs.items() if ref.startswith("refs/heads/") and sha == head
    )
    if branches:
        branch = "refs/heads/main" if "refs/heads/main" in branches else branches[0]
        git(apm_dir, ["symbolic-ref", "HEAD", branch])
    else:
        git(apm_dir, ["update-ref", "--no-deref", "HEAD", head])
    git(apm_dir, ["reset", "--quiet", "--hard", "HEAD"])

    commits = int(git(apm_dir, ["rev-list", "--all", "--count"]).strip())
    _check_count(src, contents.manifest["counts"], "commits", commits)
    try:
        committed = _plan_files(apm_dir)
    except ArchiveError as err:
        raise ArchiveError(f"{src}: {err}") from None
    if committed != contents.plan_files:
        differ = sorted(
            name
            for name in committed.keys() | contents.plan_files.keys()
            if committed.get(name) != contents.plan_files.get(name)
        )
        raise ArchiveError(
            f"{src}: the plan files don't match the history in {BUNDLE_FILE}: {', '.join(differ)}"
        )


def _restore_store(
    src: Path,
    apm_dir: Path,
    runs: list[tuple[Run, list[RunEvent]]],
    proposals: list[Proposal],
) -> None:
    """Runs, events and proposals into a new database, with their original ids, times and
    sequence numbers. The database is created at the current schema, so runs from an older one
    land the way the store's own migration would leave them.
    """
    store = Store(apm_dir)
    try:
        for run, events in runs:
            store.restore_run(run)
            for event in events:
                store.restore_event(run.id, event)
        for proposal in proposals:
            store.restore_proposal(proposal)
    except (sqlite3.Error, TypeError, ValueError) as err:
        raise ArchiveError(f"{src}: cannot restore its runs: {err}") from None
