import json
import subprocess
import sys
from collections.abc import Callable

import pytest

from apm.core.git import log
from apm.core.ids import handle
from apm.core.model import Kind, Status
from apm.core.ops import (
    NewNode,
    attach_delivery,
    create_nodes,
    decompose,
    delete_node,
    edit_body,
    merge_nodes,
    move_node,
    set_status,
    update_node,
)
from apm.core.project import Project, load_graph
from apm.core.review_lock import (
    TaskUnderReview,
    read_review_lock,
    release_review_lock,
    review_lock_path,
    take_review_lock,
    under_review,
)

RUN = "run-1"


@pytest.fixture
def locked(project: Project) -> tuple[str, str]:
    """Two top-level tasks; the first is under review. Returns (locked, other)."""
    task, other = create_nodes(project, [NewNode(title="Task"), NewNode(title="Other")]).created
    take_review_lock(project.apm_dir, task, RUN)
    return task, other


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_the_lock_can_be_taken_read_and_released(project: Project) -> None:
    task = create_nodes(project, [NewNode(title="Task")]).created[0]
    assert not under_review(project.apm_dir, task)
    take_review_lock(project.apm_dir, task, RUN)
    assert review_lock_path(project.apm_dir, task).name == f"{task}.json"
    owner = read_review_lock(project.apm_dir, task)
    assert owner is not None
    assert owner["run"] == RUN
    assert isinstance(owner["pid"], int)
    assert under_review(project.apm_dir, task)
    release_review_lock(project.apm_dir, task)
    assert not under_review(project.apm_dir, task)
    release_review_lock(project.apm_dir, task)  # releasing twice is fine


def test_a_marker_whose_pid_is_dead_does_not_count(project: Project) -> None:
    task = create_nodes(project, [NewNode(title="Task")]).created[0]
    take_review_lock(project.apm_dir, task, RUN, pid=dead_pid())
    assert not under_review(project.apm_dir, task)
    update_node(project, task, title="Renamed")


def test_a_marker_from_another_host_counts(project: Project) -> None:
    task = create_nodes(project, [NewNode(title="Task")]).created[0]
    path = review_lock_path(project.apm_dir, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": dead_pid(), "hostname": "elsewhere", "run": RUN}))
    assert under_review(project.apm_dir, task)
    path.write_text("not json")
    assert not under_review(project.apm_dir, task)


OPERATIONS: dict[str, Callable[[Project, str, str], object]] = {
    "edit_body": lambda p, t, _: edit_body(p, t, "## Intent\n\nNew.\n"),
    "update": lambda p, t, _: update_node(p, t, title="New title"),
    "move": lambda p, t, o: move_node(p, t, o),
    "delete": lambda p, t, _: delete_node(p, t),
    "merge_into": lambda p, t, o: merge_nodes(p, t, [o]),
    "merge_away": lambda p, t, o: merge_nodes(p, o, [t]),
    "decompose": lambda p, t, _: decompose(p, t, [NewNode(title="Child")]),
    "create_under": lambda p, t, _: create_nodes(p, [NewNode(title="Child", parent=t)]),
}


@pytest.mark.parametrize("operation", OPERATIONS)
def test_changes_to_a_task_under_review_are_refused(
    project: Project, locked: tuple[str, str], operation: str
) -> None:
    task, other = locked
    commits = len(log(project.apm_dir))
    with pytest.raises(TaskUnderReview, match=f"{handle(task)} is under review") as err:
        OPERATIONS[operation](project, task, other)
    assert err.value.http_status == 409
    assert len(log(project.apm_dir)) == commits


def test_deleting_an_ancestor_of_a_task_under_review_is_refused(project: Project) -> None:
    parent = create_nodes(project, [NewNode(title="Parent")]).created[0]
    child = create_nodes(project, [NewNode(title="Child", parent=parent)]).created[0]
    take_review_lock(project.apm_dir, child, RUN)
    with pytest.raises(TaskUnderReview, match=handle(child)):
        delete_node(project, parent, recursive=True)


def test_status_and_delivery_still_change_under_review(
    project: Project, locked: tuple[str, str]
) -> None:
    task, other = locked
    set_status(project, task, Status.IN_PROGRESS)
    attach_delivery(project, task, branch="apm/task")
    update_node(project, other, kind=Kind.DECISION)
    node = load_graph(project)[task]
    assert node.status is Status.IN_PROGRESS
    assert node.delivery is not None
    assert node.delivery.branch == "apm/task"
