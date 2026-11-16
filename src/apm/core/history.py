"""What happened to a node: the plan commits that list it in `APM-Nodes` (plans.md §3.5)."""

from dataclasses import dataclass

from apm.core.git import log
from apm.core.model import NodeId
from apm.core.project import Project


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    sha: str
    time: int
    """Unix seconds."""
    subject: str
    op: str
    actor: str
    run: str | None


def node_history(project: Project, node_id: NodeId, limit: int = 50) -> list[HistoryEntry]:
    """Newest first."""
    entries = []
    for entry in log(project.apm_dir):
        if node_id not in entry.trailers.get("APM-Nodes", "").split(","):
            continue
        entries.append(
            HistoryEntry(
                sha=entry.sha,
                time=entry.time,
                subject=entry.subject,
                op=entry.trailers.get("APM-Op", ""),
                actor=entry.trailers.get("APM-Actor", ""),
                run=entry.trailers.get("APM-Run"),
            )
        )
        if len(entries) >= limit:
            break
    return entries
