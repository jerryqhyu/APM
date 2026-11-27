import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from apm.core.model import Delivery, Kind, Status
from apm.core.store import FeatureReport, Store, TaskReview, content_fingerprint
from tests.core.build import A, B, node


def test_the_fingerprint_follows_title_kind_and_body_only() -> None:
    base = node(A)
    print_ = content_fingerprint(base, "body")
    assert content_fingerprint(base, "body") == print_
    assert content_fingerprint(node(A, status=Status.DONE), "body") == print_
    assert content_fingerprint(node(A, delivery=Delivery(repo="r", branch="b")), "body") == print_
    assert content_fingerprint(base, "body!") != print_
    assert content_fingerprint(node(A, kind=Kind.DECISION), "body") != print_
    assert content_fingerprint(replace(base, title="other"), "body") != print_


def test_task_reviews_give_the_latest_and_count_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert store.latest_task_review(A) is None
    store.add_task_review(TaskReview(A, "f1", "error", {}, created=1.0))
    store.add_task_review(TaskReview(A, "f1", "error", {}, run_id="r1", created=2.0))
    store.add_task_review(TaskReview(A, "f2", "error", {}, created=3.0))
    latest = TaskReview(A, "f2", "too_big", {"split": ["é"]}, run_id="r2", created=4.0)
    store.add_task_review(latest)
    store.add_task_review(TaskReview(B, "f9", "pass", {}, created=5.0))
    assert Store(tmp_path).latest_task_review(A) == latest
    assert store.count_errors(A, "f1") == 2
    assert store.count_errors(A, "f2") == 1
    assert store.count_errors(B, "f9") == 0


def test_an_existing_database_gains_the_task_reviews_table(tmp_path: Path) -> None:
    run = Store(tmp_path).create_run(node_id=A, agent="a")
    with sqlite3.connect(tmp_path / ".cache" / "apm.db") as db:
        db.execute("DROP TABLE task_reviews")
    store = Store(tmp_path)
    assert store.get_run(run.id) == run
    store.add_task_review(TaskReview(A, "f", "unclear", {"why": "?"}))
    assert store.latest_task_review(A) is not None


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


def test_a_resumed_run_keeps_only_the_cost_it_added(tmp_path: Path) -> None:
    store = Store(tmp_path)
    first = store.create_run(node_id=A, agent="apm-assistant", session_id="s")
    assert store.finish_run(first.id, "succeeded", cost_usd=1.0).cost_usd == 1.0
    second = store.create_run(node_id=A, agent="apm-assistant", session_id="s")
    # claude reports the session's total so far: $3 is $1 before and $2 now.
    assert store.finish_run(second.id, "succeeded", cost_usd=3.0).cost_usd == 2.0
    third = store.create_run(node_id=A, agent="apm-assistant", session_id="s")
    assert store.finish_run(third.id, "failed", cost_usd=3.0).cost_usd == 0.0


def test_fresh_runs_keep_the_cost_they_report(tmp_path: Path) -> None:
    store = Store(tmp_path)
    one = store.create_run(node_id=A, agent="apm-critic")
    two = store.create_run(node_id=A, agent="apm-critic")
    assert store.finish_run(one.id, "succeeded", cost_usd=1.5).cost_usd == 1.5
    assert store.finish_run(two.id, "succeeded", cost_usd=0.5).cost_usd == 0.5
    # A session id claude gives back only when the run ends is fresh too.
    three = store.create_run(node_id=A, agent="apm-critic")
    assert store.finish_run(three.id, "succeeded", cost_usd=0.75, session_id="new").cost_usd == 0.75
    # A resumed session whose count started afresh keeps the reported cost whole.
    four = store.create_run(node_id=A, agent="apm-critic", session_id=one.session_id)
    assert store.finish_run(four.id, "succeeded", cost_usd=0.25).cost_usd == 0.25


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
    store.restore([(r, source.list_events(r.id)) for r in (run, open_run)], [pending, resolved])

    assert store.get_run(run.id) == run
    assert store.get_run(open_run.id) == open_run
    assert store.list_runs() == source.list_runs()
    assert store.list_events(run.id) == source.list_events(run.id)
    assert store.get_proposal(pending.id) == pending
    assert store.get_proposal(resolved.id) == resolved
    assert store.list_proposals() == source.list_proposals()
    # New events carry on numbering after the restored ones.
    assert store.add_event(run.id, {"type": "more"}).seq == 3

    # Taken ids are refused, and a refused restore writes nothing.
    fresh = source.create_run(node_id=A, agent="c")
    with pytest.raises(sqlite3.IntegrityError):
        store.restore([(fresh, []), (run, [])], [])
    assert fresh.id not in {r.id for r in store.list_runs()}
    with pytest.raises(sqlite3.IntegrityError):
        store.restore([], [pending])


def test_an_acceptance_keeps_the_children_as_accepted(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert store.acceptance("p1") is None
    store.record_acceptance("p1", [{"key": "a", "title": "Renamed é"}], [A, B])
    store.record_acceptance("p2", None, [A])
    first = Store(tmp_path).acceptance("p1")
    assert first is not None
    assert (first.children, first.created) == ([{"key": "a", "title": "Renamed é"}], (A, B))
    second = store.acceptance("p2")
    assert second is not None
    assert (second.children, second.created) == (None, (A,))


def test_the_latest_report_of_a_feature(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert store.latest_report(A) is None
    old = FeatureReport(A, "r1", "feature/x", "docs/x.md", "s1", None, "# Old", "", "brief", 1.0)
    new = FeatureReport(
        A, "r2", "feature/x", "docs/x.md", None, "push refused", "# é", "One line.", "standard", 2.0
    )
    other = FeatureReport(B, None, "feature/y", "docs/y.md", "s3", None, "# Y", "", "detailed", 3.0)
    for report in (old, new, other):
        store.add_report(report)
    assert Store(tmp_path).latest_report(A) == new
    assert store.latest_report(B) == other
