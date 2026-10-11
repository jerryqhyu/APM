from pathlib import Path

import pytest

from apm.core.files import body_path, read_file_if_exists, write_file_atomic

ID = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"


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
