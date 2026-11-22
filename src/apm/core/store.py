"""The local database (plans.md §4): `.apm/.cache/apm.db`, never committed.

It holds what only matters on this machine: agent runs, their event logs, proposals waiting
for a decision, task reviews, and the state of eager builds. Deleting it loses those and nothing
about the plan itself.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import astuple, dataclass, field
from pathlib import Path
from typing import Any, Literal

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


class Store:
    def __init__(self, apm_dir: Path) -> None:
        self.path = apm_dir / CACHE_DIR / DB_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        """A short-lived connection per call: safe from any thread or process."""
        db = sqlite3.connect(self.path, timeout=10.0)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

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
        with self._db() as db:
            db.execute(
                "INSERT INTO runs(id, node_id, agent, session_id, status, started, prompt,"
                " parent_run, pid, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.node_id,
                    run.agent,
                    run.session_id,
                    run.status,
                    run.started,
                    run.prompt,
                    run.parent_run,
                    run.pid,
                    run.mode,
                ),
            )
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
        with self._db() as db:
            db.execute(
                "UPDATE runs SET status = ?, ended = ?, cost_usd = ?, error = ?,"
                " session_id = COALESCE(?, session_id) WHERE id = ?",
                (status, time.time(), cost_usd, error, session_id, run_id),
            )
        return self.get_run(run_id)

    def set_prompt(self, run_id: str, prompt: str) -> Run:
        """Replaces a run's first message before it is sent: the runner adds what it works out
        once the run has started (an implementer's base branch).
        """
        with self._db() as db:
            db.execute("UPDATE runs SET prompt = ? WHERE id = ?", (prompt, run_id))
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run {run_id}")
        return _run(row)

    def list_runs(
        self, *, node_id: NodeId | None = None, status: RunStatus | None = None, limit: int = 100
    ) -> list[Run]:
        """Newest first."""
        clauses, params = _where(node_id=node_id, status=status)
        with self._db() as db:
            rows = db.execute(
                f"SELECT * FROM runs {clauses} ORDER BY started DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [_run(row) for row in rows]

    def add_event(self, run_id: str, data: dict[str, Any]) -> RunEvent:
        with self._db() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            event = RunEvent(seq=row[0], ts=time.time(), data=data)
            db.execute(
                "INSERT INTO run_events(run_id, seq, ts, json) VALUES(?, ?, ?, ?)",
                (run_id, event.seq, event.ts, json.dumps(data, ensure_ascii=False)),
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
            id=uuid.uuid4().hex[:12],
            node_id=node_id,
            run_id=run_id,
            data=data,
            review=review,
            status="pending",
            created=time.time(),
            resolved=None,
        )
        with self._db() as db:
            db.execute(
                "INSERT INTO proposals(id, node_id, run_id, json, review, status, created)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.id,
                    node_id,
                    run_id,
                    json.dumps(data, ensure_ascii=False),
                    None if review is None else json.dumps(review, ensure_ascii=False),
                    proposal.status,
                    proposal.created,
                ),
            )
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal:
        with self._db() as db:
            row = db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown proposal {proposal_id}")
        return _proposal(row)

    def list_proposals(
        self, *, node_id: NodeId | None = None, status: ProposalStatus | None = None
    ) -> list[Proposal]:
        """Newest first."""
        clauses, params = _where(node_id=node_id, status=status)
        with self._db() as db:
            rows = db.execute(
                f"SELECT * FROM proposals {clauses} ORDER BY created DESC", params
            ).fetchall()
        return [_proposal(row) for row in rows]

    def resolve_proposal(self, proposal_id: str, status: ProposalStatus) -> Proposal:
        """Marks a pending proposal accepted or rejected. Raises `ValueError` if it was already
        resolved, so one proposal can't be accepted twice.
        """
        with self._db() as db:
            done = db.execute(
                "UPDATE proposals SET status = ?, resolved = ? WHERE id = ? AND status = 'pending'",
                (status, time.time(), proposal_id),
            )
        proposal = self.get_proposal(proposal_id)
        if done.rowcount == 0:
            raise ValueError(f"proposal {proposal_id} is already {proposal.status}")
        return proposal

    def reopen_proposal(self, proposal_id: str) -> None:
        """Puts a proposal back to pending, when accepting it failed."""
        with self._db() as db:
            db.execute(
                "UPDATE proposals SET status = 'pending', resolved = NULL WHERE id = ?",
                (proposal_id,),
            )

    # --- task reviews (not archived: an imported project's tasks are simply reviewed again) ---

    def add_task_review(self, review: TaskReview) -> None:
        values = list(astuple(review))
        values[3] = json.dumps(review.data, ensure_ascii=False)
        with self._db() as db:
            db.execute("INSERT INTO task_reviews VALUES(?, ?, ?, ?, ?, ?)", values)

    def latest_task_review(self, node_id: NodeId) -> TaskReview | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM task_reviews WHERE node_id = ? ORDER BY created DESC, rowid DESC",
                (node_id,),
            ).fetchone()
        return None if row is None else TaskReview(**{**row, "data": json.loads(row["data"])})

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

    # --- eager builds (not archived: they describe branches in this machine's clone) ---

    def save_eager_build(self, build: EagerBuild) -> EagerBuild:
        """Records a feature's eager build, replacing the one before."""
        values = list(astuple(build))
        values[7] = json.dumps(list(build.tasks))
        values[10] = json.dumps(list(build.warnings), ensure_ascii=False)
        values[11] = (
            None if build.pending is None else json.dumps(build.pending, ensure_ascii=False)
        )
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO eager_builds VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return build

    def eager_build(self, feature_id: NodeId) -> EagerBuild | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM eager_builds WHERE feature_id = ?", (feature_id,)
            ).fetchone()
        return None if row is None else _eager_build(row)

    def eager_builds(self) -> list[EagerBuild]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM eager_builds ORDER BY feature_id").fetchall()
        return [_eager_build(row) for row in rows]

    def save_eager_slices(self, slices: Iterable[EagerSlice]) -> None:
        """Records task slices, replacing what was recorded for the same tasks."""
        with self._db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO eager_slices VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (astuple(s) for s in slices),
            )

    def eager_slices(self, feature_id: NodeId) -> list[EagerSlice]:
        """A feature's slices in chain order."""
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM eager_slices WHERE feature_id = ? ORDER BY position",
                (feature_id,),
            ).fetchall()
        return [EagerSlice(*row) for row in rows]

    def eager_slice_of(self, node_id: NodeId) -> EagerSlice | None:
        """The slice a task was delivered as, if an eager build split it."""
        with self._db() as db:
            row = db.execute("SELECT * FROM eager_slices WHERE node_id = ?", (node_id,)).fetchone()
        return None if row is None else EagerSlice(*row)

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
                db.execute(
                    "INSERT INTO runs(id, node_id, agent, session_id, status, started, ended,"
                    " cost_usd, error, prompt, parent_run, pid, mode)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run.id,
                        run.node_id,
                        run.agent,
                        run.session_id,
                        run.status,
                        run.started,
                        run.ended,
                        run.cost_usd,
                        run.error,
                        run.prompt,
                        run.parent_run,
                        run.pid,
                        run.mode,
                    ),
                )
                db.executemany(
                    "INSERT INTO run_events(run_id, seq, ts, json) VALUES(?, ?, ?, ?)",
                    ((run.id, e.seq, e.ts, json.dumps(e.data, ensure_ascii=False)) for e in events),
                )
            db.executemany(
                "INSERT INTO proposals(id, node_id, run_id, json, review, status, created,"
                " resolved) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        p.id,
                        p.node_id,
                        p.run_id,
                        json.dumps(p.data, ensure_ascii=False),
                        None if p.review is None else json.dumps(p.review, ensure_ascii=False),
                        p.status,
                        p.created,
                        p.resolved,
                    )
                    for p in proposals
                ),
            )


def _where(**filters: object) -> tuple[str, tuple[object, ...]]:
    used = {key: value for key, value in filters.items() if value is not None}
    if not used:
        return "", ()
    return "WHERE " + " AND ".join(f"{key} = ?" for key in used), tuple(used.values())


def _run(row: sqlite3.Row) -> Run:
    return Run(**{key: row[key] for key in row.keys()})  # noqa: SIM118 (Row isn't a dict)


def _eager_build(row: sqlite3.Row) -> EagerBuild:
    return EagerBuild(
        feature_id=row["feature_id"],
        round=row["round"],
        branch=row["branch"],
        base=row["base"],
        base_sha=row["base_sha"],
        sha=row["sha"],
        state=row["state"],
        tasks=tuple(json.loads(row["tasks"])),
        run_id=row["run_id"],
        error=row["error"],
        warnings=tuple(json.loads(row["warnings"])),
        pending=None if row["pending"] is None else json.loads(row["pending"]),
        updated=row["updated"],
    )


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        node_id=row["node_id"],
        run_id=row["run_id"],
        data=json.loads(row["json"]),
        review=None if row["review"] is None else json.loads(row["review"]),
        status=row["status"],
        created=row["created"],
        resolved=row["resolved"],
    )
