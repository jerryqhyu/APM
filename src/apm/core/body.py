"""nodes/<id>.md: prose only, plain markdown with no frontmatter (plans.md §3.3)."""

from collections.abc import Iterable
from pathlib import Path

from apm.core.files import body_path, read_file_if_exists, write_file_atomic
from apm.core.model import NodeId


def render_body(*, intent: str = "", acceptance: Iterable[str] = (), notes: str = "") -> str:
    """The body for a new node: Intent / Acceptance criteria / Notes."""

    def section(heading: str, content: str) -> str:
        content = content.strip()
        return f"## {heading}\n\n{content}\n" if content else f"## {heading}\n"

    criteria = "\n".join(f"- [ ] {a.strip()}" for a in acceptance)
    return "\n".join(
        [
            section("Intent", intent),
            section("Acceptance criteria", criteria),
            section("Notes", notes),
        ]
    )


def read_body(apm_dir: Path, node_id: NodeId) -> str:
    """A node's body, or "" if it has no body file."""
    return read_file_if_exists(body_path(apm_dir, node_id)) or ""


def write_body(apm_dir: Path, node_id: NodeId, text: str) -> None:
    write_file_atomic(body_path(apm_dir, node_id), text)
