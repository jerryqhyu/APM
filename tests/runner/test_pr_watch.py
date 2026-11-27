"""`apm serve` watches pull requests waiting for a merge, and reconciles when one merges."""

from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.pr_status import PrStatuses
from apm.core.project import Project
from apm.runner.auto import PR_CHECK_S, AutoQueue
from tests.fake_gh import FakeGh
from tests.runner.test_runner import Harness


def test_a_merge_makes_the_next_reconcile_due_at_once(project: Project, fake_gh: FakeGh) -> None:
    url = fake_gh.add(9)
    (task,) = create_nodes(project, [NewNode(title="Ledger")]).created
    set_status(project, task, Status.IN_PROGRESS)
    attach_delivery(project, task, branch="apm/ledger", pr=url)
    now = [1000.0]
    harness = Harness(project)
    queue = AutoQueue(harness.runner, clock=lambda: now[0])
    queue.prs = PrStatuses(project.code_dir, clock=lambda: now[0])
    queue.next_reconcile = now[0] + 300

    assert queue.check_prs() is False
    assert [e["pr"]["state"] for e in harness.events if e["type"] == "pr.status"] == ["open"]
    assert queue.check_prs() is False  # not due again yet: GitHub isn't asked
    fake_gh.review(9, reviews=[{"state": "APPROVED", "user": {"login": "ana"}}])
    now[0] += PR_CHECK_S
    assert queue.check_prs() is False
    assert harness.events[-1]["pr"]["review"] == "approved"

    fake_gh.merge(9, "abc")
    now[0] += PR_CHECK_S
    assert queue.check_prs() is True
    assert harness.events[-1] == {
        "type": "pr.status",
        "node_id": task,
        "pr": harness.events[-1]["pr"],
    }
    assert harness.events[-1]["pr"]["state"] == "merged"
    assert queue.next_reconcile == 0.0  # the reconcile that marks it done runs at once
