from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from apm.core.graph import Graph
from apm.core.model import Delivery, Kind, Node, Status
from apm.core.validate import InvariantError, Issue, validate
from tests.core.build import A, B, C, D, E, F, G, node
from tests.strategies import graphs

DONE, DROPPED, IN_PROGRESS = Status.DONE, Status.DROPPED, Status.IN_PROGRESS


def errors(*nodes: Node) -> list[tuple[str, tuple[str, ...]]]:
    return [(i.code, i.nodes) for i in validate(Graph(nodes)).errors]


def warnings(*nodes: Node) -> list[tuple[str, tuple[str, ...]]]:
    return [(i.code, i.nodes) for i in validate(Graph(nodes)).warnings]


def test_a_sound_graph_has_no_issues() -> None:
    review = node(E, A, deps=[B, C], kind=Kind.REVIEW)
    report = validate(Graph([node(A), node(B, A), node(C, A, deps=[B]), node(D, deps=[C]), review]))
    assert report.ok
    assert report.errors == ()
    assert report.warnings == ()


def test_i1_the_parent_must_exist() -> None:
    assert errors(node(B, A)) == [("I1", (B,))]


def test_i1_containment_has_no_cycles() -> None:
    assert errors(node(A, C), node(B, A), node(C, B)) == [("I1", (A, B, C))]
    assert errors(node(A, A)) == [("I1", (A,))]


def test_i2_dependencies_have_no_cycles() -> None:
    assert errors(node(A, deps=[B]), node(B, deps=[C]), node(C, deps=[A])) == [("I2", (A, B, C))]
    assert errors(node(A, deps=[A])) == [("I2", (A,))]


def test_i2_reports_each_tangle_once_with_an_example_cycle() -> None:
    # A, B and C are tangled by two overlapping cycles; D depends on itself.
    report = validate(
        Graph([node(A, deps=[B, C]), node(B, deps=[A]), node(C, deps=[A]), node(D, deps=[D])])
    )
    assert [(i.code, i.nodes) for i in report.errors] == [("I2", (A, B, C)), ("I2", (D,))]
    assert report.errors[0].message.startswith("dependency cycle: 0000000a → ")
    assert report.errors[1].message == "dependency cycle: 0000000d → 0000000d"


def test_i3_no_dependency_on_an_ancestor_or_a_descendant() -> None:
    assert errors(node(A), node(B, A), node(C, B, deps=[A])) == [("I3", (C, A))]
    assert errors(node(A, deps=[C]), node(B, A), node(C, B)) == [("I3", (A, C))]
    # A sibling, or a cousin, is fine.
    assert errors(node(A), node(B, A), node(C, A, deps=[B]), node(D, deps=[B])) == []


def test_i4_every_dependency_exists() -> None:
    assert errors(node(A, deps=[B])) == [("I4", (A,))]


def test_i7_only_leaves_carry_delivery() -> None:
    delivery = Delivery(repo="app")
    assert errors(node(A, delivery=delivery), node(B, A)) == [("I7", (A,))]
    assert errors(node(A), node(B, A, delivery=delivery)) == []


def test_i6_warns_about_a_done_parent_with_unfinished_children() -> None:
    assert warnings(node(A, status=DONE), node(B, A), node(C, A, status=IN_PROGRESS)) == [
        ("I6", (A, B, C))
    ]
    # Done and dropped children are finished; a parent that isn't done isn't checked.
    assert warnings(node(A, status=DONE), node(B, A, status=DONE), node(C, A, status=DROPPED)) == []
    assert warnings(node(A), node(B, A), node(C, A, deps=[B], kind=Kind.REVIEW)) == []
    assert validate(Graph([node(A, status=DONE), node(B, A)])).ok


def test_i8_a_review_step_is_a_leaf_inside_a_feature_and_one_per_feature() -> None:
    review = Kind.REVIEW
    assert errors(node(A, kind=review)) == [("I8", (A,))]  # not inside a feature
    assert errors(node(A), node(B, A, kind=review), node(C, B)) == [("I8", (B,))]  # has a child
    two = errors(node(A), node(B, A), node(C, A, kind=review), node(D, A, kind=review))
    assert two == [("I8", (A, C, D))]
    assert errors(node(A), node(B, A), node(C, A, deps=[B], kind=review)) == []


def test_i9_warns_about_a_feature_without_a_review_step() -> None:
    assert warnings(node(A), node(B, A)) == [("I9", (A,))]
    # A done or dropped feature isn't asked for one: it's finished.
    assert warnings(node(A, status=DONE), node(B, A, status=DONE)) == []
    assert warnings(node(F, status=DROPPED), node(G, F)) == []


def test_reports_every_problem_not_just_the_first() -> None:
    codes = [code for code, _ in errors(node(A, B, deps=[C]), node(D, deps=[D]))]
    assert codes == ["I1", "I2", "I4"]


def test_invariant_error_lists_its_issues() -> None:
    issue = Issue("I4", "0000000a: dependency x doesn't exist", (A,))
    err = InvariantError([issue])
    assert err.issues == (issue,)
    assert str(err) == "I4: 0000000a: dependency x doesn't exist"


@given(graphs())
def test_generated_graphs_pass(nodes: list[Node]) -> None:
    assert validate(Graph(nodes)).errors == ()


@given(graphs(), st.data())
def test_a_dependency_on_an_ancestor_is_always_caught(
    nodes: list[Node], data: st.DataObject
) -> None:
    children = [n for n in nodes if n.parent is not None]
    if not children:
        return
    child = data.draw(st.sampled_from(children))
    ancestor = data.draw(st.sampled_from(Graph(nodes).ancestors(child.id)))
    broken = [replace(n, depends_on=(ancestor,)) if n.id == child.id else n for n in nodes]
    assert ("I3", (child.id, ancestor)) in errors(*broken)


@given(graphs(), st.data())
def test_closing_a_dependency_loop_is_always_caught(nodes: list[Node], data: st.DataObject) -> None:
    with_deps = [n for n in nodes if n.depends_on]
    if not with_deps:
        return
    src = data.draw(st.sampled_from(with_deps))
    dst = data.draw(st.sampled_from(src.depends_on))
    # dst now depends on src as well: a two-node cycle.
    broken = [replace(n, depends_on=(*n.depends_on, src.id)) if n.id == dst else n for n in nodes]
    tangled = [i for i in validate(Graph(broken)).errors if i.code == "I2"]
    assert any({src.id, dst} <= set(i.nodes) for i in tangled)
