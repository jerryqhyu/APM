"""The plan repo's files (plans.md §3.1, §3.3): paths, atomic writes, the graph file, and node
bodies. A body is `nodes/<id>.md`: prose only, plain markdown with no frontmatter.
"""

import os
import secrets
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from apm.core.ids import is_node_id
from apm.core.model import Node, NodeId
from apm.core.ndjson import GraphParseError, ParseIssue, parse_graph, serialize_graph

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
    try:
        text = graph_path(apm_dir).read_text(encoding="utf-8", newline="")
    except FileNotFoundError:
        raise GraphParseError(
            [
                ParseIssue(
                    0, "the file is missing: restore it with `git -C .apm checkout graph.ndjson`"
                )
            ]
        ) from None
    return parse_graph(text, GRAPH_FILE)


_CACHE: dict[tuple[Path, Callable[..., Any]], tuple[tuple[int, int, int], Any]] = {}
_CACHE_SIZE = 32


def cached_parse[T](path: Path, parse: Callable[[Path], T]) -> T:
    """`parse(path)`, reused until the file changes (its inode, mtime or size). The result must be
    treated as read-only. A missing file is parsed every time, so `parse` reports it.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return parse(path)
    key, stamp = (path, parse), (st.st_ino, st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    value = parse(path)
    if len(_CACHE) >= _CACHE_SIZE:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (stamp, value)
    return value


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


def render_body(
    *,
    intent: str = "",
    context: str = "",
    approach: str = "",
    acceptance: Iterable[str] = (),
    out_of_scope: Iterable[str] = (),
    notes: str = "",
) -> str:
    """The body for a new node: Intent, Context, Approach, Acceptance criteria, Out of scope and
    Notes. Intent, Acceptance criteria and Notes are always there, as a template to fill in; the
    others only when they say something.
    """

    def section(heading: str, content: str) -> str:
        content = content.strip()
        return f"## {heading}\n\n{content}\n" if content else f"## {heading}\n"

    criteria = "\n".join(f"- [ ] {a.strip()}" for a in acceptance if a.strip())
    exclusions = "\n".join(f"- {o.strip()}" for o in out_of_scope if o.strip())
    sections = [
        ("Intent", intent, True),
        ("Context", context, False),
        ("Approach", approach, False),
        ("Acceptance criteria", criteria, True),
        ("Out of scope", exclusions, False),
        ("Notes", notes, True),
    ]
    return "\n".join(section(h, text) for h, text, always in sections if always or text.strip())


def read_body(apm_dir: Path, node_id: NodeId) -> str:
    """A node's body, or "" if it has no body file."""
    return read_file_if_exists(body_path(apm_dir, node_id)) or ""


def write_body(apm_dir: Path, node_id: NodeId, text: str) -> None:
    write_file_atomic(body_path(apm_dir, node_id), text)
