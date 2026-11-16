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
