"""Splitting a task in place: a task review's split replaces the task in its feature, so the plan
never gains a level from it.
"""

import pytest

from apm.core.git import log
from apm.core.ids import handle
from apm.core.model import Status
from apm.core.mutate import OpError
from apm.core.ops import NewNode, attach_delivery, create_nodes, link, set_status, split_task
from apm.core.project import Project, load_graph


def plan(project: Project) -> dict[str, str]:
    """A feature with a task that needs `before` and is needed by `after` and by `elsewhere`."""
    feature, elsewhere = create_nodes(
        project, [NewNode(title="Cost view"), NewNode(title="Elsewhere")]
    ).created
    before, task, after = create_nodes(
        project,
        [
            NewNode(title="Before", parent=feature),
            NewNode(title="Ledger", parent=feature, tags=["cost"]),
            NewNode(title="After", parent=feature),
        ],
    ).created
    link(project, task, before)
    link(project, after, task)
    link(project, elsewhere, task)
    return {
        "feature": feature,
        "before": before,
        "task": task,
        "after": after,
        "elsewhere": elsewhere,
    }


PARTS = [
    NewNode(title="Sum runs", key="sum"),
    NewNode(title="Freeze on merge", key="freeze", depends_on_keys=["sum"]),
    NewNode(title="Live total", key="live", depends_on_keys=["sum"], tags=["ui"]),
]


def test_the_parts_replace_the_task_in_its_feature(project: Project) -> None:
    n = plan(project)
    commits = len(log(project.apm_dir))
    result = split_task(project, n["task"], PARTS)
    graph = load_graph(project)
    sum_, freeze, live = result.created

    assert n["task"] not in graph
    assert {graph[p].parent for p in result.created} == {n["feature"]}
    assert all(graph.is_leaf(p) for p in result.created)
    assert graph[sum_].depends_on == (n["before"],)  # the first part takes over what it needed
    assert graph[freeze].depends_on == (sum_,)
    # What needed the task needs the parts nothing else in the split needs.
    assert set(graph[n["after"]].depends_on) == {freeze, live}
    assert set(graph[n["elsewhere"]].depends_on) == {freeze, live}
    assert graph[live].tags == ("cost", "ui")
    step = graph.review_step(n["feature"])
    assert step is not None
    assert set(graph[step].depends_on) == {n["before"], n["after"], *result.created}
    assert len(log(project.apm_dir)) == commits + 1
    assert log(project.apm_dir)[0].subject == f"split {handle(n['task'])}: Ledger → 3 tasks"


def test_only_a_task_that_has_not_started_can_be_split(project: Project) -> None:
    n = plan(project)
    with pytest.raises(OpError, match="is a feature, not a task"):
        split_task(project, n["feature"], PARTS)
    with pytest.raises(OpError, match="decompose it instead"):
        split_task(project, n["elsewhere"], PARTS)  # top level: no feature to split it into
    set_status(project, n["task"], Status.IN_PROGRESS)
    with pytest.raises(OpError, match="has started"):
        split_task(project, n["task"], PARTS)
    set_status(project, n["task"], Status.TODO)
    attach_delivery(project, n["task"], branch="apm/ledger")
    with pytest.raises(OpError, match="has started"):
        split_task(project, n["task"], PARTS)
