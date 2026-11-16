"""Paths inside the plan repo (plans.md §3.1) and atomic file I/O."""

import os
import secrets
from collections.abc import Iterable
from pathlib import Path

from apm.core.ids import is_node_id
from apm.core.model import Node, NodeId
from apm.core.ndjson import parse_graph, serialize_graph

GRAPH_FILE = "graph.ndjson"
NODES_DIR = "nodes"
CONFIG_FILE = "apm.yaml"


def graph_path(apm_dir: Path) -> Path:
    return apm_dir / GRAPH_FILE


def config_path(apm_dir: Path) -> Path:
    return apm_dir / CONFIG_FILE


def body_path(apm_dir: Path, node_id: NodeId) -> Path:
    """Path of a node's body. Raises on anything but a valid ID, so input can't escape `nodes/`."""
    if not is_node_id(node_id):
        raise ValueError(f"not a node id: {node_id!r}")
    return apm_dir / NODES_DIR / f"{node_id}.md"


def read_graph_file(apm_dir: Path) -> list[Node]:
    return parse_graph(graph_path(apm_dir).read_text(encoding="utf-8", newline=""), GRAPH_FILE)


def write_graph_file(apm_dir: Path, nodes: Iterable[Node]) -> None:
    """Serializes before touching the disk, so a refused write leaves the existing file alone."""
    write_file_atomic(graph_path(apm_dir), serialize_graph(nodes))


def write_file_atomic(path: Path, data: str) -> None:
    """Writes via a temp file in the same directory and a rename, so readers in other processes see
    either the old file or the new one, never a partial write. Creates parent directories. Line
    endings are written exactly as given.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_file_if_exists(path: Path) -> str | None:
    """Reads a UTF-8 file with line endings untouched, or returns `None` if it doesn't exist."""
    try:
        return path.read_text(encoding="utf-8", newline="")
    except FileNotFoundError:
        return None
