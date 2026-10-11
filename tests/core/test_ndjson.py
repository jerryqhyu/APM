import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.provisional import urls

from apm.core.model import Delivery, External, Kind, Node, Status
from apm.core.ndjson import GraphParseError, InvalidNodeError, parse_graph, serialize_graph
from tests.strategies import node_ids

FIXTURES = Path(__file__).parent.parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", newline="")


ROOT = "0192f2c8-1a0d-7e33-8c41-5b2a9d0e7f10"
MODEL = "0192e9d0-55aa-7c12-9f00-aa11bb22cc33"
STREAM = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"
LAYOUT = "01930a00-0000-7000-8000-00000000d0d0"

# The graph in fixtures/graph.golden.ndjson, in id order.
GOLDEN = [
    Node(id=MODEL, parent=ROOT, title="Graph model and file I/O", status=Status.DONE),
    Node(id=ROOT, title="APM v1", status=Status.IN_PROGRESS),
    Node(
        id=STREAM,
        parent=ROOT,
        title="Stream agent output to the UI",
        status=Status.IN_PROGRESS,
        depends_on=(MODEL,),
        delivery=Delivery(
            repo="app",
            branch="apm/1c0b9a44-stream-output",
            commits=("a1b2c3d", "e4f5a6b7c8d9"),
            pr="https://github.com/jerry/app/pull/12",
        ),
    ),
    Node(
        id=LAYOUT,
        parent=ROOT,
        title='Pick a layout engine — "elkjs" vs dagre',
        kind=Kind.DECISION,
        status=Status.DROPPED,
        external=External(
            provider="github", id="42", synced_at="2026-09-20T12:00:00Z", synced_hash="abc123"
        ),
        variant_of=STREAM,
    ),
]


def test_parses_the_golden_file() -> None:
    assert parse_graph(fixture("graph.golden.ndjson")) == GOLDEN


def test_serializes_the_golden_graph_byte_for_byte_whatever_the_input_order() -> None:
    assert serialize_graph(GOLDEN) == fixture("graph.golden.ndjson")
    assert serialize_graph(reversed(GOLDEN)) == fixture("graph.golden.ndjson")


def test_canonicalizes_hand_written_input() -> None:
    """Any line order, CRLF, blank lines, nulls, missing kind/status, duplicate deps."""
    text = fixture("graph.handwritten.ndjson")
    assert "\r\n" in text
    assert parse_graph(text) == GOLDEN
    assert serialize_graph(parse_graph(text)) == fixture("graph.golden.ndjson")


def test_ignores_a_leading_byte_order_mark() -> None:
    assert parse_graph("﻿" + fixture("graph.golden.ndjson")) == GOLDEN
    assert parse_graph("﻿" + fixture("graph.handwritten.ndjson")) == GOLDEN


def test_treats_an_empty_file_as_an_empty_graph() -> None:
    assert parse_graph("") == []
    assert serialize_graph([]) == ""


def errors_for(text: str) -> list[str]:
    with pytest.raises(GraphParseError) as caught:
        parse_graph(text)
    return [f"{i.line}: {i.message}" for i in caught.value.issues]


OK = f'{{"id":"{ROOT}","title":"ok"}}'


def test_reports_every_bad_line_with_its_line_number() -> None:
    text = "\n".join(
        [
            OK,
            "{not json",
            f'{{"id":"{MODEL}","title":"x","status":"blocked"}}',
            f'{{"id":"{STREAM}","title":"x","colour":"red"}}',
            "",
            f'{{"id":"{ROOT}","title":"dup"}}',
        ]
    )
    issues = errors_for(text)
    assert len(issues) == 4
    assert issues[0].startswith("2: invalid JSON")
    assert issues[1].startswith("3: status: ")
    assert re.match(r"4: .*colour", issues[2])
    assert issues[3] == f"6: duplicate id {ROOT} (first on line 1)"


@pytest.mark.parametrize(
    ("line", "pattern"),
    [
        (f'{{"id":"{ROOT.upper()}","title":"x"}}', r"id: must be a lowercase UUIDv7"),
        ('{"id":"0192f2c8-1a0d-4e33-8c41-5b2a9d0e7f10","title":"x"}', r"id: must be"),
        (f'{{"id":"{ROOT}","title":"x","depends_on":["nope"]}}', r"depends_on\[0\]: "),
        (f'{{"id":"{ROOT}","title":"  "}}', r"title: must not be blank"),
        (f'{{"id":"{ROOT}","title":"a\\nb"}}', r"title: must be a single line"),
        (f'{{"id":"{ROOT}"}}', r"title: "),
        (f'{{"id":"{ROOT}","title":5}}', r"title: "),
        ("[1,2]", r"expected a JSON object"),
        (
            f'{{"id":"{ROOT}","title":"x","delivery":{{"repo":"app","commits":["zz"]}}}}',
            r"delivery\.commits\[0\]: must be a hex commit SHA",
        ),
        (f'{{"id":"{ROOT}","title":"x","delivery":{{"branch":"b"}}}}', r"delivery\.repo: "),
        (
            f'{{"id":"{ROOT}","title":"x","delivery":{{"repo":"app","pr":"12"}}}}',
            r"delivery\.pr: must be an http\(s\) URL",
        ),
        (f'{{"id":"{ROOT}","title":"\\ud800"}}', r"invalid JSON"),
    ],
    ids=[
        "an uppercase id",
        "a v4 id",
        "a bad dependency",
        "a blank title",
        "a multi-line title",
        "a missing title",
        "a number for a title",
        "a non-object",
        "a bad commit",
        "a delivery without repo",
        "a non-URL pr",
        "a lone surrogate",
    ],
)
def test_rejects(line: str, pattern: str) -> None:
    issues = errors_for(line)
    assert any(re.search(pattern, issue) for issue in issues), issues


def test_names_the_source_file_in_the_message() -> None:
    with pytest.raises(GraphParseError, match=r"^plan/graph\.ndjson:1: invalid JSON"):
        parse_graph("{", "plan/graph.ndjson")


def test_omits_null_and_empty_values_but_always_writes_kind_and_status() -> None:
    node = Node(id=ROOT, title="t", delivery=Delivery(repo="app", branch="", commits=()))
    assert serialize_graph([node]) == (
        f'{{"id":"{ROOT}","title":"t","kind":"work","status":"todo","delivery":{{"repo":"app"}}}}\n'
    )


BASE = Node(id=ROOT, title="t")


@pytest.mark.parametrize(
    ("patch", "pattern"),
    [
        ({"title": "a\nb"}, r"title: must be a single line"),
        ({"title": "  "}, r"title: must not be blank"),
        ({"title": "\ud800"}, r"lone surrogate"),
        ({"id": "0192f2c8-1a0d-4e33-8c41-5b2a9d0e7f10"}, r"id: must be a lowercase UUIDv7"),
        ({"parent": "nope"}, r"parent: must be a lowercase UUIDv7"),
        ({"depends_on": ("nope",)}, r"depends_on\[0\]: "),
        ({"delivery": Delivery(repo="app", commits=("zz",))}, r"delivery\.commits\[0\]: "),
        ({"delivery": Delivery(repo="")}, r"delivery\.repo: "),
        ({"delivery": Delivery(repo="app", pr="12")}, r"delivery\.pr: "),
        ({"status": "blocked"}, r"status: "),
    ],
    ids=[
        "a multi-line title",
        "a blank title",
        "a lone surrogate",
        "a non-v7 id",
        "a bad parent",
        "a bad dependency",
        "a non-hex commit",
        "an empty delivery repo",
        "a non-URL pr",
        "an unknown status",
    ],
)
def test_refuses_to_write_a_node_it_could_not_read_back(
    patch: dict[str, Any], pattern: str
) -> None:
    bad = dataclasses.replace(BASE, **patch)
    with pytest.raises(InvalidNodeError, match=pattern):
        serialize_graph([GOLDEN[0], bad])


def test_refuses_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate id"):
        serialize_graph([GOLDEN[0], GOLDEN[0]])


# Canonical nodes: what parse_graph returns. References needn't resolve: that's P03's job.
titles = st.text(min_size=1, max_size=20).filter(
    lambda t: t.strip() and not re.search(r"[\r\n]", t)
)
words = st.text(min_size=1, max_size=10)
shas = st.text(alphabet="0123456789abcdef", min_size=7, max_size=40)
canonical_nodes = st.builds(
    Node,
    id=node_ids(),
    parent=st.none() | node_ids(),
    title=titles,
    kind=st.sampled_from(Kind),
    status=st.sampled_from(Status),
    depends_on=st.lists(node_ids(), max_size=4, unique=True).map(lambda d: tuple(sorted(d))),
    delivery=st.none()
    | st.builds(
        Delivery,
        repo=words,
        branch=st.none() | words,
        commits=st.lists(shas, max_size=3).map(tuple),
        pr=st.none() | urls(),
    ),
    external=st.none()
    | st.builds(
        External,
        provider=words,
        id=words,
        synced_at=st.none() | words,
        synced_hash=st.none() | words,
    ),
    variant_of=st.none() | node_ids(),
)
graphs = st.lists(canonical_nodes, max_size=15, unique_by=lambda n: n.id)


@given(graphs)
def test_parse_of_serialize_is_identity(graph: list[Node]) -> None:
    assert parse_graph(serialize_graph(graph)) == sorted(graph, key=lambda n: n.id)


@given(graphs.flatmap(lambda g: st.tuples(st.just(g), st.permutations(g))))
def test_serialization_is_byte_stable_and_independent_of_input_order(
    graphs: tuple[list[Node], list[Node]],
) -> None:
    graph, shuffled = graphs
    text = serialize_graph(graph)
    assert serialize_graph(shuffled) == text
    assert serialize_graph(parse_graph(text)) == text


# Deliberately loose: values that are often invalid, to exercise the write-side check.
loose_ids = node_ids() | st.uuids().map(str) | st.text(max_size=8)
loose_text = st.text(max_size=8)
loose_nodes = st.builds(
    Node,
    id=loose_ids,
    parent=st.none() | loose_ids,
    title=titles | loose_text | st.sampled_from(["", " ", "a\nb", "a\rb"]),
    kind=st.sampled_from(Kind),
    status=st.sampled_from(Status),
    depends_on=st.lists(loose_ids, max_size=3).map(tuple),
    delivery=st.none()
    | st.builds(
        Delivery,
        repo=loose_text,
        commits=st.lists(shas | loose_text, max_size=2).map(tuple),
        pr=st.none() | urls() | loose_text,
    ),
)


@settings(max_examples=500)
@given(st.lists(loose_nodes, max_size=5, unique_by=lambda n: n.id))
def test_whatever_serialize_writes_parse_reads_back_or_it_refuses(graph: list[Node]) -> None:
    try:
        text = serialize_graph(graph)
    except InvalidNodeError:
        return
    assert serialize_graph(parse_graph(text)) == text
