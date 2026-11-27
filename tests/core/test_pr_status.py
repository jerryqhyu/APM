"""A pull request's state on GitHub, read through `gh api` and kept for a while."""

from pathlib import Path

from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.pr_status import PrStatuses, awaiting_merge, read_pr_status
from apm.core.project import Project, load_graph
from tests.fake_gh import FakeGh

APPROVED = {"state": "APPROVED", "user": {"login": "ana"}}
COMMENTED = {"state": "COMMENTED", "user": {"login": "bo"}}
CHANGES = {"state": "CHANGES_REQUESTED", "user": {"login": "bo"}}
PASSED = {"status": "completed", "conclusion": "success"}
FAILED = {"status": "completed", "conclusion": "failure"}
RUNNING = {"status": "in_progress", "conclusion": None}


def status(fake_gh: FakeGh, tmp_path: Path, number: int) -> tuple[str, str, str]:
    found = read_pr_status(tmp_path, f"https://github.com/o/r/pull/{number}", 1.0)
    assert found is not None
    return found.state, found.review, found.checks


def test_reviews_and_checks_say_where_a_pull_request_stands(
    fake_gh: FakeGh, tmp_path: Path
) -> None:
    fake_gh.add(1)
    fake_gh.review(1, reviews=[APPROVED, COMMENTED], checks=[PASSED, PASSED])
    assert status(fake_gh, tmp_path, 1) == ("open", "approved", "passing")
    # The latest word of each reviewer counts, and one failed check fails them all.
    fake_gh.review(1, reviews=[APPROVED, CHANGES], checks=[PASSED, RUNNING, FAILED])
    assert status(fake_gh, tmp_path, 1) == ("open", "changes_requested", "failing")
    fake_gh.review(1, reviews=[], checks=[PASSED, RUNNING])
    assert status(fake_gh, tmp_path, 1) == ("open", "waiting", "running")
    fake_gh.review(1)
    assert status(fake_gh, tmp_path, 1) == ("open", "waiting", "none")


def test_merged_and_closed_pull_requests_need_no_more_questions(
    fake_gh: FakeGh, tmp_path: Path
) -> None:
    fake_gh.add(2, state="closed", merged=True, merge_commit="abc")
    fake_gh.add(3, state="closed")
    calls = len(fake_gh.calls)
    assert status(fake_gh, tmp_path, 2) == ("merged", "waiting", "none")
    assert status(fake_gh, tmp_path, 3) == ("closed", "waiting", "none")
    assert len(fake_gh.calls) == calls + 2  # one call each: no reviews or checks asked for


def test_statuses_are_kept_and_the_last_known_survives_a_failure(
    fake_gh: FakeGh, tmp_path: Path
) -> None:
    url = fake_gh.add(4)
    now = [100.0]
    statuses = PrStatuses(tmp_path, clock=lambda: now[0])
    assert statuses.known(url) is None
    first = statuses.get(url)
    assert first is not None
    calls = len(fake_gh.calls)
    assert statuses.get(url) is first  # still fresh: GitHub isn't asked
    assert len(fake_gh.calls) == calls
    now[0] += 120
    fake_gh.fail()
    assert statuses.get(url) is first  # GitHub can't be asked: what was known
    assert read_pr_status(tmp_path, "not a url", 0) is None


def test_only_delivered_work_in_progress_waits_for_a_merge(project: Project) -> None:
    with_pr, branch_only, todo = create_nodes(
        project, [NewNode(title="A"), NewNode(title="B"), NewNode(title="C")]
    ).created
    for node in (with_pr, branch_only):
        set_status(project, node, Status.IN_PROGRESS)
    attach_delivery(project, with_pr, branch="a", pr="https://github.com/o/r/pull/5")
    attach_delivery(project, branch_only, branch="b")
    assert awaiting_merge(load_graph(project)) == {with_pr: "https://github.com/o/r/pull/5"}
    assert todo not in awaiting_merge(load_graph(project))
