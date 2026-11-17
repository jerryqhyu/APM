"""UUIDv7 IDs and short handles (plans.md §3.7).

The handle is the LAST 8 hex chars. The leading chars are the timestamp, so nodes created close
together share them; the trailing chars are random.
"""

import re
import uuid
from collections.abc import Iterable, Sequence
from typing import Literal

from apm.core.model import NodeId

UUID_V7_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

HANDLE_LENGTH = 8
MIN_HANDLE_INPUT = 4
"""Shortest suffix `resolve_handle` accepts."""

_HEX_RE = re.compile(r"[0-9a-f]+")


def new_id() -> NodeId:
    """A new ID. IDs from one process sort in creation order, even within the same millisecond."""
    return str(uuid.uuid7())


def is_node_id(value: str) -> bool:
    return UUID_V7_RE.fullmatch(value) is not None


def handle(node_id: NodeId) -> str:
    return node_id[-HANDLE_LENGTH:]


def handles(ids: Iterable[NodeId]) -> dict[NodeId, str]:
    """Display handles for a set of IDs: the last 8 hex chars, lengthened only for IDs whose last 8
    collide with another's, until they're unique. (32 random bits: collisions are rare but real in
    large projects.)
    """
    # IDs sharing a longer suffix also share the last 8, so only same-handle groups need work.
    groups: dict[str, list[NodeId]] = {}
    for node_id in ids:
        groups.setdefault(handle(node_id), []).append(node_id)
    out: dict[NodeId, str] = {}
    for short, group in groups.items():
        if len(group) == 1:
            out[group[0]] = short
            continue
        hexes = [_hex(node_id) for node_id in group]
        for i, h in enumerate(hexes):
            length = HANDLE_LENGTH
            while length < len(h) and any(
                j != i and other.endswith(h[-length:]) for j, other in enumerate(hexes)
            ):
                length += 1
            out[group[i]] = h[-length:]
    return out


type HandleErrorCode = Literal["invalid", "unknown", "ambiguous"]


class HandleError(ValueError):
    def __init__(
        self, code: HandleErrorCode, value: str, message: str, candidates: Sequence[NodeId] = ()
    ) -> None:
        super().__init__(message)
        self.code: HandleErrorCode = code
        self.value = value
        self.candidates: tuple[NodeId, ...] = tuple(candidates)
        """For `ambiguous`: every matching ID."""


def resolve_handle(value: str, ids: Iterable[NodeId]) -> NodeId:
    """Resolves user input to one of `ids`. Accepts any unambiguous hex suffix of at least 4 chars,
    or a full ID with or without hyphens, in any case.
    """
    needle = value.strip().lower().replace("-", "")
    if _HEX_RE.fullmatch(needle) is None or not MIN_HANDLE_INPUT <= len(needle) <= 32:
        raise HandleError(
            "invalid",
            value,
            f'invalid handle "{value}": expected at least {MIN_HANDLE_INPUT} hex characters '
            "from the end of a node ID",
        )
    matches = [node_id for node_id in ids if _hex(node_id).endswith(needle)]
    if not matches:
        raise HandleError("unknown", value, f'no node matches handle "{value}"')
    if len(matches) > 1:
        shown = handles(matches)
        listed = ", ".join(shown[node_id] for node_id in matches)
        raise HandleError(
            "ambiguous",
            value,
            f'handle "{value}" matches {len(matches)} nodes: {listed}',
            matches,
        )
    return matches[0]


def _hex(node_id: NodeId) -> str:
    return node_id.replace("-", "")
