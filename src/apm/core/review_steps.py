"""Review steps (plans.md §3.9): every node with children ends in one.

A node with children is a feature. Its review step is one more child, of kind `review`, that
depends on all of its other children, so it becomes ready only once they are done: then the
feature reviewer checks the feature branch against the feature's description. The step's delivery
is the feature branch, and its pull request is the feature's way into the branch above it.

`maintain` keeps that true on every change to the plan (`mutate` calls it before validating):

- A node that gets children, and isn't done or dropped, gets a review step.
- A review step depends on exactly its feature's other children that aren't dropped: added ones
  are added, removed or dropped ones removed.
- A review step that is all its feature has left is removed, unless it already holds work.
"""

from dataclasses import replace
from typing import TYPE_CHECKING

from apm.core.files import render_body
from apm.core.ids import new_id
from apm.core.model import Kind, Node, NodeId, Status
from apm.core.ndjson import canonical_ids

if TYPE_CHECKING:
    from apm.core.mutate import Draft

TITLE = "Feature review"


def review_body() -> str:
    return render_body(
        intent=(
            "Check that the feature is really complete before its branch goes on: the feature"
            " reviewer reads the feature branch and checks it against the feature's description,"
            " criterion by criterion. Gaps it finds become follow-up tasks."
        ),
        acceptance=[
            "The feature reviewer's verdict on the feature branch is complete",
            "The feature branch's pull request is merged",
        ],
    )


def maintain(draft: Draft) -> None:
    graph = draft.graph
    involved: set[NodeId] = set()
    for node_id in draft.touched:
        involved.add(node_id)
        if node_id in draft and (parent := draft[node_id].parent) is not None:
            involved.add(parent)

    for parent in sorted(involved):
        if parent not in draft:
            continue
        children = graph.children(parent)
        others = [c for c in children if not graph.is_review(c)]
        step = graph.review_step(parent)
        if step is None and others and draft[parent].status not in (Status.DONE, Status.DROPPED):
            step = new_id()
            draft.put(Node(id=step, title=TITLE, parent=parent, kind=Kind.REVIEW), auto=True)
            draft.set_body(step, review_body())
        elif step is not None and not others and _empty(draft[step]):
            draft.remove(step)

    graph = draft.graph
    for node in list(graph.nodes.values()):
        if node.kind is not Kind.REVIEW or node.parent is None or node.parent not in graph:
            continue
        siblings = [
            c
            for c in graph.children(node.parent)
            if c != node.id and not graph.is_review(c) and graph[c].status is not Status.DROPPED
        ]
        wanted = canonical_ids(siblings)
        if node.depends_on != wanted:
            draft.put(replace(node, depends_on=wanted))


def _empty(step: Node) -> bool:
    """A review step that has done nothing yet: no feature branch, still to do."""
    return step.delivery is None and step.status is Status.TODO
