import re
from pathlib import Path

import pytest

from apm.core.model import Delivery, External, Kind, Node, Status
from apm.core.ndjson import GraphParseError, parse_graph

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


def test_canonicalizes_hand_written_input() -> None:
    """Any line order, CRLF, blank lines, nulls, missing kind/status, duplicate deps."""
    text = fixture("graph.handwritten.ndjson")
    assert "\r\n" in text
    assert parse_graph(text) == GOLDEN


def test_ignores_a_leading_byte_order_mark() -> None:
    assert parse_graph("﻿" + fixture("graph.golden.ndjson")) == GOLDEN
    assert parse_graph("﻿" + fixture("graph.handwritten.ndjson")) == GOLDEN


def test_treats_an_empty_file_as_an_empty_graph() -> None:
    assert parse_graph("") == []


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
