"""The local database (plans.md §4): `.apm/.cache/apm.db`, never committed.

It holds what only matters on this machine: agent runs, their event logs, proposals waiting
for a decision, task reviews, and the state of eager builds. Deleting it loses those and nothing
about the plan itself.
"""

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal, get_origin

from apm.core.lock import CACHE_DIR
from apm.core.model import Node, NodeId

DB_FILE = "apm.db"
SCHEMA_VERSION = 2
PROJECT = ""
"""The `node_id` of a run that is about the whole project rather than one node."""

type RunStatus = Literal["running", "succeeded", "failed", "cancelled"]
type ProposalStatus = Literal["pending", "accepted", "rejected"]
type ReviewVerdict = Literal["pass", "too_big", "unclear", "error"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started REAL NOT NULL,
    ended REAL,
    cost_usd REAL,
    error TEXT,
    prompt TEXT NOT NULL DEFAULT '',
    parent_run TEXT,
    pid INTEGER,
    mode TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_node ON runs(node_id, started);
CREATE TABLE IF NOT EXISTS run_events(
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY(run_id, seq)
);
CREATE TABLE IF NOT EXISTS proposals(
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    run_id TEXT,
    json TEXT NOT NULL,
    review TEXT,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    resolved REAL
);
CREATE INDEX IF NOT EXISTS proposals_node ON proposals(node_id, created);
CREATE TABLE IF NOT EXISTS task_reviews(
    node_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    verdict TEXT NOT NULL,
    data TEXT NOT NULL,
    run_id TEXT,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_reviews_node ON task_reviews(node_id, created);
CREATE TABLE IF NOT EXISTS follow_ups(node_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS acceptances(
    proposal_id TEXT PRIMARY KEY,
    children TEXT,
    created_nodes TEXT NOT NULL,
    accepted REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports(
    node_id TEXT NOT NULL,
    run_id TEXT,
    branch TEXT NOT NULL,
    path TEXT NOT NULL,
    sha TEXT,
    error TEXT,
    markdown TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_node ON reports(node_id, created);
CREATE TABLE IF NOT EXISTS eager_builds(
    feature_id TEXT PRIMARY KEY,
    round INTEGER NOT NULL,
    branch TEXT NOT NULL,
    base TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    sha TEXT,
    state TEXT NOT NULL,
    tasks TEXT NOT NULL,
    run_id TEXT,
    error TEXT,
    warnings TEXT NOT NULL,
    pending TEXT,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS eager_slices(
    feature_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    branch TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    parent_sha TEXT NOT NULL,
    tree TEXT NOT NULL,
    patch_id TEXT,
    lines INTEGER,
    landed TEXT,
    PRIMARY KEY(feature_id, node_id)
);
"""


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    node_id: NodeId
    agent: str
    session_id: str
    status: RunStatus
    started: float
    ended: float | None
    cost_usd: float | None
    error: str | None
    prompt: str
    parent_run: str | None
    """The run this one follows up on: a critic's decomposer, or the run a message continues."""
    pid: int | None
    """The process hosting the run, so a run whose host died can be told from a live one."""
    mode: str = ""
    """The permission mode it ran in (see `apm.runner.agents.MODES`)."""


@dataclass(frozen=True, slots=True)
class RunEvent:
    seq: int
    ts: float
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Proposal:
    id: str
    node_id: NodeId
    run_id: str | None
    data: dict[str, Any]
    """The decomposition, as the agent returned it (see `apm.core.proposals`)."""
    review: dict[str, Any] | None
    """The critic's review, if one ran."""
    status: ProposalStatus
    created: float
    resolved: float | None


@dataclass(frozen=True, slots=True)
class TaskReview:
    """A task's review, for one version of its content (see `content_fingerprint`)."""

    node_id: NodeId
    fingerprint: str
    verdict: ReviewVerdict
    data: dict[str, Any]
    """What the reviewer returned with its verdict."""
    run_id: str | None = None
    created: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class Acceptance:
    """What you accepted from a proposal: the children as you left them, and the nodes made."""

    proposal_id: str
    children: list[dict[str, Any]] | None
    """The children you accepted, if you passed a list (edited or trimmed, or as proposed);
    None when the proposal was accepted whole without one.
    """
    created: tuple[NodeId, ...]
    accepted: float


@dataclass(frozen=True, slots=True)
class FeatureReport:
    """A feature report APM wrote (see `apm.core.report`). The latest one per feature counts."""

    node_id: NodeId
    """The feature."""
    run_id: str | None
    branch: str
    """The feature branch it was committed to."""
    path: str
    """Where it is in the code repo, e.g. `docs/features/search-1c0b9a44.md`."""
    sha: str | None
    """The commit that added it to the branch; None if it couldn't be committed (see `error`)."""
    error: str | None
    markdown: str
    """The file, as committed (or as it would have been)."""
    summary: str
    detail: str
    """`brief`, `standard` or `detailed`."""
    created: float = field(default_factory=time.time)


type EagerState = Literal["building", "splitting", "split", "failed", "split_failed"]


@dataclass(frozen=True, slots=True)
class EagerBuild:
    """The latest eager build of a feature (see `apm.core.eager`)."""

    feature_id: NodeId
    round: int
    """1 for the first build, then one more for every rebuild: each has its own eager branch."""
    branch: str
    """The eager branch the builder works on."""
    base: str
    """The branch the build starts from and the chain of task pull requests goes into: the
    feature branch."""
    base_sha: str
    """The commit the eager branch started from."""
    sha: str | None
    """The eager branch's tip that was split, once it was."""
    state: EagerState
    tasks: tuple[NodeId, ...]
    """The tasks this round builds, in the order of the chain."""
    run_id: str | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()
    """What the split couldn't do as asked, e.g. a slice over its task's size limit."""
    pending: dict[str, Any] | None = None
    """A task pull request that changed since the split, as redo detection last saw it."""
    updated: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class EagerSlice:
    """One task's part of an eager build: the commit APM put on the task's branch."""

    feature_id: NodeId
    node_id: NodeId
    position: int
    """Its place in the chain, from 0: each task's branch is built on the one before."""
    branch: str
    commit: str
    """The task branch's tip as APM left it (after a redo, the commit that rebuilt it)."""
    parent: str
    """The commit it is built on: the previous task's, or the base for the first."""
    tree: str
    """`commit`'s tree: the code as it is once this task and every one before it are in."""
    patch_id: str | None
    """The patch id of the slice's own change (`parent` to `commit`), to recognise it once it is
    squashed or rebased into the feature branch."""
    lines: int | None = None
    """Roughly how many lines of source the slice changes."""
    landed: str | None = None
    """The commit that brought it into the feature branch as split, once it did."""


def content_fingerprint(node: Node, body: str) -> str:
    """Identifies what a task review reads: title, kind and body, not status or delivery."""
    content = json.dumps([node.title, node.kind, body], ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _Table:
    """How a record class maps onto its table: columns in table order, the field each holds, and
    which fields are stored as JSON (a tuple field is read back as a tuple)."""

    name: str
    columns: tuple[str, ...]
    fields: tuple[str, ...]
    json: frozenset[str]
    tuples: frozenset[str]

    def row(self, record: object) -> tuple[object, ...]:
        return tuple(
            _dump(getattr(record, f)) if f in self.json else getattr(record, f) for f in self.fields
        )

    def record(self, cls: type, row: sqlite3.Row) -> Any:
        values = {}
        for column, f in zip(self.columns, self.fields, strict=True):
            value = row[column]
            if f in self.json and value is not None:
                value = json.loads(value)
                value = tuple(value) if f in self.tuples else value
            values[f] = value
        return cls(**values)


def _table(cls: type, name: str, json_fields: str = "", **renamed: str) -> _Table:
    names = tuple(f.name for f in fields(cls))
    as_json = frozenset(json_fields.split())
    tuples = frozenset(f.name for f in fields(cls) if get_origin(f.type) is tuple) & as_json
    return _Table(name, tuple(renamed.get(n, n) for n in names), names, as_json, tuples)


_TABLES: dict[type, _Table] = {
    Run: _table(Run, "runs"),
    Proposal: _table(Proposal, "proposals", "data review", data="json"),
    TaskReview: _table(TaskReview, "task_reviews", "data"),
    Acceptance: _table(Acceptance, "acceptances", "children created", created="created_nodes"),
    FeatureReport: _table(FeatureReport, "reports"),
    EagerBuild: _table(EagerBuild, "eager_builds", "tasks warnings pending"),
    EagerSlice: _table(EagerSlice, "eager_slices", commit="commit_sha", parent="parent_sha"),
}


def _dump(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(list(value) if isinstance(value, tuple) else value, ensure_ascii=False)


class Store:
    def __init__(self, apm_dir: Path) -> None:
        self.path = apm_dir / CACHE_DIR / DB_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._db() as db:
            db.executescript(_SCHEMA)
            # Databases from before version 2 have no `mode` column.
            if "mode" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT ''")
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        """A transaction on this thread's connection, opened on first use and then reused. Nested
        calls join the outer transaction, so it stays all or nothing.
        """
        local = self._local
        if getattr(local, "db", None) is None:
            local.db = sqlite3.connect(self.path, timeout=10.0)
            local.db.row_factory = sqlite3.Row
            local.db.execute("PRAGMA journal_mode=WAL")
            local.depth = 0
        if local.depth:
            yield local.db
            return
        local.depth = 1
        try:
            with local.db:
                yield local.db
        finally:
            local.depth = 0

    def _insert(self, records: Iterable[object], *, replace: bool = False) -> None:
        with self._db() as db:
            for record in records:
                table = _TABLES[type(record)]
                marks = ", ".join("?" * len(table.columns))
                verb = "INSERT OR REPLACE" if replace else "INSERT"
                columns = ", ".join(table.columns)
                db.execute(
                    f"{verb} INTO {table.name}({columns}) VALUES({marks})", table.row(record)
                )

    def _select[T](self, cls: type[T], tail: str = "", params: Iterable[object] = ()) -> list[T]:
        table = _TABLES[cls]
        with self._db() as db:
            rows = db.execute(f"SELECT * FROM {table.name} {tail}", tuple(params)).fetchall()
        return [table.record(cls, row) for row in rows]

    def _one[T](self, cls: type[T], tail: str, params: Iterable[object]) -> T | None:
        found = self._select(cls, tail + " LIMIT 1", params)
        return found[0] if found else None

    def _update(self, sql: str, params: Iterable[object]) -> int:
        with self._db() as db:
            return db.execute(sql, tuple(params)).rowcount

    # --- runs ---

    def create_run(
        self,
        *,
        node_id: NodeId,
        agent: str,
        prompt: str = "",
        session_id: str | None = None,
        parent_run: str | None = None,
        pid: int | None = None,
        mode: str = "",
    ) -> Run:
        run = Run(
            id=uuid.uuid4().hex[:12],
            node_id=node_id,
            agent=agent,
            session_id=session_id or str(uuid.uuid4()),
            status="running",
            started=time.time(),
            ended=None,
            cost_usd=None,
            error=None,
            prompt=prompt,
            parent_run=parent_run,
            pid=pid,
            mode=mode,
        )
        self._insert([run])
        return run

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        cost_usd: float | None = None,
        error: str | None = None,
        session_id: str | None = None,
    ) -> Run:
        self._update(
            "UPDATE runs SET status = ?, ended = ?, cost_usd = ?, error = ?,"
            " session_id = COALESCE(?, session_id) WHERE id = ?",
            (status, time.time(), cost_usd, error, session_id, run_id),
        )
        return self.get_run(run_id)

    def set_prompt(self, run_id: str, prompt: str) -> Run:
        """Replaces a run's first message before it is sent: the runner adds what it works out
        once the run has started (an implementer's base branch).
        """
        self._update("UPDATE runs SET prompt = ? WHERE id = ?", (prompt, run_id))
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        run = self._one(Run, "WHERE id = ?", (run_id,))
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        return run

    def list_runs(
        self, *, node_id: NodeId | None = None, status: RunStatus | None = None, limit: int = 100
    ) -> list[Run]:
        """Newest first."""
        where, params = _where(node_id=node_id, status=status)
        return self._select(Run, f"{where} ORDER BY started DESC LIMIT ?", (*params, limit))

    def add_event(self, run_id: str, data: dict[str, Any]) -> RunEvent:
        with self._db() as db:
            seq = db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            event = RunEvent(seq=seq, ts=time.time(), data=data)
            db.execute(
                "INSERT INTO run_events(run_id, seq, ts, json) VALUES(?, ?, ?, ?)",
                (run_id, seq, event.ts, _dump(data)),
            )
        return event

    def list_events(self, run_id: str, after: int = 0) -> list[RunEvent]:
        with self._db() as db:
            rows = db.execute(
                "SELECT seq, ts, json FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after),
            ).fetchall()
        return [RunEvent(row["seq"], row["ts"], json.loads(row["json"])) for row in rows]

    # --- proposals ---

    def create_proposal(
        self,
        *,
        node_id: NodeId,
        data: dict[str, Any],
        run_id: str | None = None,
        review: dict[str, Any] | None = None,
    ) -> Proposal:
        proposal = Proposal(
            uuid.uuid4().hex[:12], node_id, run_id, data, review, "pending", time.time(), None
        )
        self._insert([proposal])
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal:
        proposal = self._one(Proposal, "WHERE id = ?", (proposal_id,))
        if proposal is None:
            raise KeyError(f"unknown proposal {proposal_id}")
        return proposal

    def list_proposals(
        self, *, node_id: NodeId | None = None, status: ProposalStatus | None = None
    ) -> list[Proposal]:
        """Newest first."""
        where, params = _where(node_id=node_id, status=status)
        return self._select(Proposal, f"{where} ORDER BY created DESC", params)

    def resolve_proposal(self, proposal_id: str, status: ProposalStatus) -> Proposal:
        """Marks a pending proposal accepted or rejected. Raises `ValueError` if it was already
        resolved, so one proposal can't be accepted twice.
        """
        done = self._update(
            "UPDATE proposals SET status = ?, resolved = ? WHERE id = ? AND status = 'pending'",
            (status, time.time(), proposal_id),
        )
        proposal = self.get_proposal(proposal_id)
        if done == 0:
            raise ValueError(f"proposal {proposal_id} is already {proposal.status}")
        return proposal

    def reopen_proposal(self, proposal_id: str) -> None:
        """Puts a proposal back to pending, when accepting it failed."""
        self._update(
            "UPDATE proposals SET status = 'pending', resolved = NULL WHERE id = ?", (proposal_id,)
        )

    # --- task reviews (not archived: an imported project's tasks are simply reviewed again) ---

    def add_task_review(self, review: TaskReview) -> None:
        self._insert([review])

    def latest_task_review(self, node_id: NodeId) -> TaskReview | None:
        return self._one(TaskReview, _LATEST, (node_id,))

    def count_errors(self, node_id: NodeId, fingerprint: str) -> int:
        """How many reviews of this content failed to give a verdict."""
        with self._db() as db:
            return db.execute(
                "SELECT COUNT(*) FROM task_reviews"
                " WHERE node_id = ? AND fingerprint = ? AND verdict = 'error'",
                (node_id, fingerprint),
            ).fetchone()[0]

    # --- follow-ups: the tasks a feature review's proposal created when accepted on its own ---

    def add_follow_ups(self, node_ids: Iterable[NodeId], proposal_id: str) -> None:
        with self._db() as db:
            db.executemany(
                "INSERT OR IGNORE INTO follow_ups VALUES(?, ?)",
                ((node_id, proposal_id) for node_id in node_ids),
            )

    def follow_ups(self) -> set[NodeId]:
        with self._db() as db:
            return {row["node_id"] for row in db.execute("SELECT node_id FROM follow_ups")}

    # --- what was accepted from a proposal (not archived, like task reviews) ---

    def record_acceptance(
        self, proposal_id: str, children: list[dict[str, Any]] | None, created: Iterable[NodeId]
    ) -> None:
        """Remembers what was accepted, so a feature report can say what you changed first."""
        acceptance = Acceptance(proposal_id, children, tuple(created), time.time())
        self._insert([acceptance], replace=True)

    def acceptance(self, proposal_id: str) -> Acceptance | None:
        return self._one(Acceptance, "WHERE proposal_id = ?", (proposal_id,))

    # --- feature reports (not archived: the report itself is committed to the feature branch) ---

    def add_report(self, report: FeatureReport) -> None:
        self._insert([report])

    def latest_report(self, node_id: NodeId) -> FeatureReport | None:
        return self._one(FeatureReport, _LATEST, (node_id,))

    # --- eager builds (not archived: they describe branches in this machine's clone) ---

    def save_eager_build(self, build: EagerBuild) -> EagerBuild:
        """Records a feature's eager build, replacing the one before."""
        self._insert([build], replace=True)
        return build

    def eager_build(self, feature_id: NodeId) -> EagerBuild | None:
        return self._one(EagerBuild, "WHERE feature_id = ?", (feature_id,))

    def eager_builds(self) -> list[EagerBuild]:
        return self._select(EagerBuild, "ORDER BY feature_id")

    def save_eager_slices(self, slices: Iterable[EagerSlice]) -> None:
        """Records task slices, replacing what was recorded for the same tasks."""
        self._insert(slices, replace=True)

    def eager_slices(self, feature_id: NodeId) -> list[EagerSlice]:
        """A feature's slices in chain order."""
        return self._select(EagerSlice, "WHERE feature_id = ? ORDER BY position", (feature_id,))

    def eager_slice_of(self, node_id: NodeId) -> EagerSlice | None:
        """The slice a task was delivered as, if an eager build split it."""
        return self._one(EagerSlice, "WHERE node_id = ?", (node_id,))

    # --- restoring (from an archive, see `apm.core.archive`) ---

    def restore(
        self, runs: Iterable[tuple[Run, Iterable[RunEvent]]], proposals: Iterable[Proposal]
    ) -> None:
        """Inserts runs with their events, and proposals, exactly as given: ids, times, sequence
        numbers and statuses. One transaction, so it's quick for long histories and all or
        nothing. Raises `sqlite3.IntegrityError` if any id (or a run's event number) is taken.
        """
        with self._db() as db:
            for run, events in runs:
                self._insert([run])
                db.executemany(
                    "INSERT INTO run_events(run_id, seq, ts, json) VALUES(?, ?, ?, ?)",
                    ((run.id, e.seq, e.ts, _dump(e.data)) for e in events),
                )
            self._insert(proposals)


_LATEST = "WHERE node_id = ? ORDER BY created DESC, rowid DESC"


def _where(**filters: object) -> tuple[str, tuple[object, ...]]:
    used = {key: value for key, value in filters.items() if value is not None}
    if not used:
        return "", ()
    return "WHERE " + " AND ".join(f"{key} = ?" for key in used), tuple(used.values())
