"""Review steps: every feature (a node with children) ends in one, and it waits on the rest."""

import pytest

from apm.core.files import read_body
from apm.core.git import log
from apm.core.model import Kind, Status
from apm.core.mutate import OpError
from apm.core.ops import (
    NewNode,
    attach_delivery,
    create_nodes,
    decompose,
    delete_node,
    link,
    merge_nodes,
    move_node,
    set_status,
    unlink,
    update_node,
)
from apm.core.project import Project, load_graph
from apm.core.review_steps import TITLE
from apm.core.validate import InvariantError, validate
from tests.conftest import tasks_of


def add(project: Project, title: str, parent: str | None = None) -> str:
    return create_nodes(project, [NewNode(title=title, parent=parent)]).created[0]


def step_of(project: Project, feature: str) -> str:
    step = load_graph(project).review_step(feature)
    assert step is not None
    return step


def test_a_node_that_gets_children_gets_a_review_step_that_waits_on_them(
    project: Project,
) -> None:
    feature = add(project, "Feature")
    assert load_graph(project).review_step(feature) is None  # no children, no review

    first, second = decompose(
        project, feature, [NewNode(title="First"), NewNode(title="Second")]
    ).created
    graph = load_graph(project)
    step = step_of(project, feature)
    node = graph[step]
    assert (node.title, node.kind, node.status) == (TITLE, Kind.REVIEW, Status.TODO)
    assert node.depends_on == (first, second)
    assert graph.children(feature) == (first, second, step)  # always last
    assert "## Acceptance criteria" in read_body(project.apm_dir, step)
    assert graph.states[step].blocked
    assert validate(graph).warnings == ()

    # Added in the same commit as the children, and not among what was "created".
    assert log(project.apm_dir)[0].trailers["APM-Nodes"].count(",") == 3

    # A task added later goes before it, and it waits on that one too.
    third = add(project, "Third", parent=feature)
    graph = load_graph(project)
    assert graph.children(feature) == (first, second, third, step)
    assert graph[step].depends_on == (first, second, third)


def test_it_waits_only_on_tasks_that_still_count(project: Project) -> None:
    feature = add(project, "Feature")
    first, second = decompose(
        project, feature, [NewNode(title="First"), NewNode(title="Second")]
    ).created
    step = step_of(project, feature)

    set_status(project, second, Status.DROPPED)
    assert load_graph(project)[step].depends_on == (first,)

    set_status(project, first, Status.DONE)
    graph = load_graph(project)
    assert graph.states[step].ready  # every remaining task is done

    move_node(project, first, None)  # moved out of the feature: not its task any more
    assert load_graph(project)[step].depends_on == ()


def test_a_review_step_left_alone_goes_unless_it_has_started(project: Project) -> None:
    feature = add(project, "Feature")
    task = add(project, "Task", parent=feature)
    step = step_of(project, feature)
    delete_node(project, task)
    assert step not in load_graph(project)
    assert tasks_of(load_graph(project), feature) == ()

    task = add(project, "Task", parent=feature)
    step = step_of(project, feature)
    attach_delivery(project, step, branch="feature/x")  # it holds the feature branch now
    delete_node(project, task)
    assert step in load_graph(project)


def test_a_finished_feature_isnt_given_one(project: Project) -> None:
    feature = add(project, "Feature")
    set_status(project, feature, Status.DONE)
    add(project, "Late addition", parent=feature)
    assert load_graph(project).review_step(feature) is None


def test_review_steps_are_apm_s_to_manage(project: Project) -> None:
    feature = add(project, "Feature")
    task = add(project, "Task", parent=feature)
    other = add(project, "Other")
    step = step_of(project, feature)

    with pytest.raises(OpError, match=r"can't be deleted\. To skip the review, set it to dropped"):
        delete_node(project, step)
    with pytest.raises(OpError, match="review steps are APM's to add and remove"):
        update_node(project, step, kind=Kind.WORK)
    with pytest.raises(OpError, match="review steps are APM's to add and remove"):
        update_node(project, task, kind=Kind.REVIEW)
    with pytest.raises(OpError, match="it stays with its feature"):
        move_node(project, step, other)
    with pytest.raises(OpError, match="depends on its feature's other children"):
        link(project, step, other)
    with pytest.raises(OpError, match="depends on its feature's other children"):
        unlink(project, step, task)
    with pytest.raises(OpError, match="APM adds a review step to every feature itself"):
        create_nodes(project, [NewNode(title="Mine", parent=other, kind=Kind.REVIEW)])
    with pytest.raises(OpError, match="a review step can't be merged"):
        merge_nodes(project, task, [step])
    with pytest.raises(InvariantError, match="I8"):
        decompose(project, step, [NewNode(title="Inside the review")])

    # Dropping it is how to skip a review; renaming it is fine.
    set_status(project, step, Status.DROPPED)
    update_node(project, step, title="Final check")
    assert load_graph(project)[step].title == "Final check"


def test_merging_features_leaves_one_review_step(project: Project) -> None:
    a = add(project, "A")
    a_task = add(project, "A task", parent=a)
    b = add(project, "B")
    b_task = add(project, "B task", parent=b)
    a_step = step_of(project, a)

    merge_nodes(project, a, [b])
    graph = load_graph(project)
    assert graph.children(a) == (a_task, b_task, a_step)
    assert graph[a_step].depends_on == (a_task, b_task)
    assert validate(graph).errors == ()


def test_a_started_review_step_stops_a_merge(project: Project) -> None:
    a = add(project, "A")
    add(project, "A task", parent=a)
    b = add(project, "B")
    add(project, "B task", parent=b)
    set_status(project, step_of(project, b), Status.IN_PROGRESS)
    with pytest.raises(OpError, match="a review step that has started"):
        merge_nodes(project, a, [b])
