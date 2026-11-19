import sqlite3
from pathlib import Path

import pytest

from apm.core.store import Store
from tests.core.build import A, B


def test_runs_are_recorded_and_finished(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run = store.create_run(node_id=A, agent="apm-decomposer", prompt="go", pid=123)
    assert run.status == "running"
    assert store.get_run(run.id) == run
    other = store.create_run(node_id=B, agent="apm-critic", parent_run=run.id)

    assert [r.id for r in store.list_runs()] == [other.id, run.id]
    assert [r.id for r in store.list_runs(node_id=A)] == [run.id]
    assert [r.id for r in store.list_runs(status="running")] == [other.id, run.id]

    done = store.finish_run(run.id, "succeeded", cost_usd=0.25, session_id="s-2")
    assert (done.status, done.cost_usd, done.session_id, done.error) == (
        "succeeded",
        0.25,
        "s-2",
        None,
    )
    assert done.ended is not None
    assert store.finish_run(other.id, "failed", error="boom").session_id == other.session_id
    assert store.list_runs(status="running") == []
    with pytest.raises(KeyError, match="unknown run"):
        store.get_run("nope")


def test_events_are_numbered_per_run(tmp_path: Path) -> None:
    store = Store(tmp_path)
    one = store.create_run(node_id=A, agent="a")
    two = store.create_run(node_id=A, agent="a")
    assert store.add_event(one.id, {"type": "assistant", "n": 1}).seq == 1
    assert store.add_event(two.id, {"type": "assistant"}).seq == 1
    assert store.add_event(one.id, {"type": "result", "text": "é"}).seq == 2
    assert [e.data for e in store.list_events(one.id)] == [
        {"type": "assistant", "n": 1},
        {"type": "result", "text": "é"},
    ]
    assert [e.seq for e in store.list_events(one.id, after=1)] == [2]


def test_a_proposal_can_only_be_resolved_once(tmp_path: Path) -> None:
    store = Store(tmp_path)
    proposal = store.create_proposal(node_id=A, data={"children": []}, review={"verdict": "accept"})
    assert store.get_proposal(proposal.id) == proposal
    assert [p.id for p in store.list_proposals(status="pending")] == [proposal.id]

    accepted = store.resolve_proposal(proposal.id, "accepted")
    assert accepted.status == "accepted"
    assert accepted.resolved is not None
    with pytest.raises(ValueError, match="already accepted"):
        store.resolve_proposal(proposal.id, "rejected")
    assert store.list_proposals(status="pending") == []
    assert [p.id for p in store.list_proposals(node_id=A)] == [proposal.id]


def test_the_database_survives_being_reopened(tmp_path: Path) -> None:
    run = Store(tmp_path).create_run(node_id=A, agent="a")
    assert Store(tmp_path).get_run(run.id) == run
    assert (tmp_path / ".cache" / "apm.db").exists()


def test_restored_records_keep_their_ids_times_numbers_and_statuses(tmp_path: Path) -> None:
    source = Store(tmp_path / "source")
    run = source.create_run(node_id=A, agent="a", prompt="go", pid=7, mode="plan")
    source.add_event(run.id, {"type": "assistant", "text": "é"})
    source.add_event(run.id, {"type": "result"})
    run = source.finish_run(run.id, "failed", cost_usd=0.1, error="boom", session_id="s")
    open_run = source.create_run(node_id=B, agent="b", parent_run=run.id)
    pending = source.create_proposal(node_id=A, data={"children": []}, run_id=run.id)
    resolved = source.create_proposal(node_id=B, data={"x": 1}, review={"verdict": "ok"})
    resolved = source.resolve_proposal(resolved.id, "rejected")

    store = Store(tmp_path / "copy")
    for restored in (run, open_run):
        store.restore_run(restored)
        for event in source.list_events(restored.id):
            store.restore_event(restored.id, event)
    for proposal in (pending, resolved):
        store.restore_proposal(proposal)

    assert store.get_run(run.id) == run
    assert store.get_run(open_run.id) == open_run
    assert store.list_runs() == source.list_runs()
    assert store.list_events(run.id) == source.list_events(run.id)
    assert store.get_proposal(pending.id) == pending
    assert store.get_proposal(resolved.id) == resolved
    assert store.list_proposals() == source.list_proposals()
    # New events carry on numbering after the restored ones.
    assert store.add_event(run.id, {"type": "more"}).seq == 3

    with pytest.raises(sqlite3.IntegrityError):
        store.restore_run(run)
    with pytest.raises(sqlite3.IntegrityError):
        store.restore_event(run.id, source.list_events(run.id)[0])
    with pytest.raises(sqlite3.IntegrityError):
        store.restore_proposal(pending)
