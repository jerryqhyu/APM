"""The node model (plans.md §2.1, §3.2).

Structure and metadata only: prose lives in nodes/<id>.md.
"""

from dataclasses import dataclass
from enum import StrEnum

type NodeId = str
"""A full, lowercase UUIDv7."""


class Kind(StrEnum):
    WORK = "work"
    DECISION = "decision"


class Status(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True, kw_only=True)
class Delivery:
    """Where a leaf's work landed in a code repo. Only leaves carry it (I7, checked in P03)."""

    repo: str
    """Key into `code.repos` in apm.yaml."""
    branch: str | None = None
    commits: tuple[str, ...] = ()
    """Commit SHAs, oldest first."""
    pr: str | None = None
    """Pull request URL."""


@dataclass(frozen=True, slots=True, kw_only=True)
class External:
    """Reserved for issue-tracker sync (v2). Parsed and preserved, otherwise unused."""

    provider: str
    id: str
    synced_at: str | None = None
    synced_hash: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Node:
    """One node, as held in memory. The serializer decides what to leave out of the file."""

    id: NodeId
    title: str
    parent: NodeId | None = None
    """`None` = top level."""
    kind: Kind = Kind.WORK
    status: Status = Status.TODO
    depends_on: tuple[NodeId, ...] = ()
    """Sorted and de-duplicated."""
    delivery: Delivery | None = None
    external: External | None = None
    variant_of: NodeId | None = None
    """Reserved for competing breakdowns (v2)."""
