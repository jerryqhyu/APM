"""The local database (plans.md §4): `.apm/.cache/apm.db`, never committed.

It holds what only matters on this machine: agent runs, their event logs, and proposals waiting
for a decision. Deleting it loses those and nothing about the plan itself.
"""

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apm.core.lock import CACHE_DIR
from apm.core.model import NodeId

DB_FILE = "apm.db"
SCHEMA_VERSION = 1

type RunStatus = Literal["running", "succeeded", "failed", "cancelled"]
type ProposalStatus = Literal["pending", "accepted", "rejected"]

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
    pid INTEGER
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


class Store:
    def __init__(self, apm_dir: Path) -> None:
        self.path = apm_dir / CACHE_DIR / DB_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
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
        )
        with self._db() as db:
            db.execute(
                "INSERT INTO runs(id, node_id, agent, session_id, status, started, prompt,"
                " parent_run, pid) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def _where(**filters: object) -> tuple[str, tuple[object, ...]]:
    used = {key: value for key, value in filters.items() if value is not None}
    if not used:
        return "", ()
    return "WHERE " + " AND ".join(f"{key} = ?" for key in used), tuple(used.values())


def _run(row: sqlite3.Row) -> Run:
    return Run(**{key: row[key] for key in row.keys()})  # noqa: SIM118 (Row isn't a dict)


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
