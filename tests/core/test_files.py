from pathlib import Path

import pytest

from apm.core.files import (
    body_path,
    read_body,
    read_file_if_exists,
    read_graph_file,
    render_body,
    write_body,
    write_file_atomic,
    write_graph_file,
)
from apm.core.model import Node
from apm.core.ndjson import GraphParseError, InvalidNodeError

ID = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"
GOOD = Node(id=ID, title="t")


def test_graph_file_round_trips_through_disk(tmp_path: Path) -> None:
    write_graph_file(tmp_path, [GOOD])
    assert read_graph_file(tmp_path) == [GOOD]
    assert [p.name for p in tmp_path.iterdir()] == ["graph.ndjson"]  # no temp files left behind


def test_a_refused_graph_write_leaves_the_existing_file_alone(tmp_path: Path) -> None:
    write_graph_file(tmp_path, [GOOD])
    before = (tmp_path / "graph.ndjson").read_bytes()
    with pytest.raises(InvalidNodeError, match="title: must be a single line"):
        write_graph_file(tmp_path, [Node(id=ID, title="two\nlines")])
    assert (tmp_path / "graph.ndjson").read_bytes() == before
    assert read_graph_file(tmp_path) == [GOOD]


def test_graph_parse_errors_name_graph_ndjson(tmp_path: Path) -> None:
    (tmp_path / "graph.ndjson").write_text("{\n")
    with pytest.raises(GraphParseError, match=r"^graph\.ndjson:1: invalid JSON"):
        read_graph_file(tmp_path)


def test_write_file_atomic_creates_parents_and_replaces_content(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "file.txt"
    write_file_atomic(path, "one")
    write_file_atomic(path, "two")
    assert path.read_text() == "two"
    assert [p.name for p in path.parent.iterdir()] == ["file.txt"]  # no temp files left behind


def test_write_file_atomic_leaves_no_temp_file_when_the_write_fails(tmp_path: Path) -> None:
    target = tmp_path / "dir"
    target.mkdir()  # a directory can't be replaced by a file
    with pytest.raises(IsADirectoryError):
        write_file_atomic(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["dir"]


def test_line_endings_round_trip_untouched(tmp_path: Path) -> None:
    path = tmp_path / "f"
    write_file_atomic(path, "a\r\nb\n")
    assert path.read_bytes() == b"a\r\nb\n"
    assert read_file_if_exists(path) == "a\r\nb\n"


def test_read_file_if_exists_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert read_file_if_exists(tmp_path / "missing") is None


def test_body_path_is_under_nodes(tmp_path: Path) -> None:
    assert body_path(tmp_path, ID) == tmp_path / "nodes" / f"{ID}.md"


@pytest.mark.parametrize("node_id", ["../../etc/passwd", ID.upper(), f"{ID}/x", ""])
def test_body_path_refuses_anything_but_a_node_id(tmp_path: Path, node_id: str) -> None:
    with pytest.raises(ValueError, match="not a node id"):
        body_path(tmp_path, node_id)


ID = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"


def test_reads_a_missing_body_as_empty(tmp_path: Path) -> None:
    assert read_body(tmp_path, ID) == ""


def test_round_trips_a_body_under_nodes(tmp_path: Path) -> None:
    write_body(tmp_path, ID, "## Intent\n\nhello\n")
    assert read_body(tmp_path, ID) == "## Intent\n\nhello\n"
    assert [p.name for p in (tmp_path / "nodes").iterdir()] == [f"{ID}.md"]


def test_renders_the_empty_template() -> None:
    assert render_body() == "## Intent\n\n## Acceptance criteria\n\n## Notes\n"


def test_context_approach_and_out_of_scope_appear_only_when_given() -> None:
    body = render_body(
        intent="Track cost.",
        context="Runs store `cost_usd`.",
        approach="1. Sum it.\n2. Freeze it.",
        acceptance=["Sums every run", " "],
        out_of_scope=["The UI: see *Costs panel*", ""],
    )
    assert body == (
        "## Intent\n\nTrack cost.\n\n"
        "## Context\n\nRuns store `cost_usd`.\n\n"
        "## Approach\n\n1. Sum it.\n2. Freeze it.\n\n"
        "## Acceptance criteria\n\n- [ ] Sums every run\n\n"
        "## Out of scope\n\n- The UI: see *Costs panel*\n\n"
        "## Notes\n"
    )
    assert "## Context" not in render_body(intent="x", context="  ")


def test_fills_intent_acceptance_criteria_and_notes() -> None:
    body = render_body(
        intent="  Show agent output live.\n",
        acceptance=["Events appear within 1s", " Survives reconnect "],
        notes="See §7.1.",
    )
    assert body == (
        "## Intent\n\nShow agent output live.\n\n"
        "## Acceptance criteria\n\n- [ ] Events appear within 1s\n- [ ] Survives reconnect\n\n"
        "## Notes\n\nSee §7.1.\n"
    )
