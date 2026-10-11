"""graph.ndjson: one node per line, sorted by id, fixed key order, null and empty values left out
(plans.md §3.2).

Parsing checks each line on its own (shape, types, ID format, duplicates). Checks that need the
whole graph, like parents existing or cycles, are the invariants in P03.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import ErrorDetails

from apm.core.ids import is_node_id
from apm.core.issues import format_issue
from apm.core.model import Delivery, External, Kind, Node, NodeId, Status


@dataclass(frozen=True, slots=True)
class ParseIssue:
    line: int
    """1-based line number."""
    message: str


class GraphParseError(ValueError):
    def __init__(self, issues: Sequence[ParseIssue], source: str = "graph.ndjson") -> None:
        super().__init__("\n".join(f"{source}:{i.line}: {i.message}" for i in issues))
        self.issues = tuple(issues)
        self.source = source


def _check_id(value: str) -> str:
    if not is_node_id(value):
        raise ValueError("must be a lowercase UUIDv7")
    return value


def _check_title(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    if "\r" in value or "\n" in value:
        raise ValueError("must be a single line")
    return value


def _check_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{7,64}", value) is None:
        raise ValueError("must be a hex commit SHA")
    return value


def _check_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or re.search(r"\s", value):
        raise ValueError("must be an http(s) URL")
    return value


Id = Annotated[str, AfterValidator(_check_id)]
NonEmpty = Annotated[str, Field(min_length=1)]


class _Line(BaseModel):
    # Optional fields accept null in hand-written files and treat it as absent.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DeliveryLine(_Line):
    repo: NonEmpty
    branch: NonEmpty | None = None
    commits: list[Annotated[str, AfterValidator(_check_sha)]] | None = None
    pr: Annotated[str, AfterValidator(_check_url)] | None = None


class ExternalLine(_Line):
    provider: NonEmpty
    id: NonEmpty
    synced_at: NonEmpty | None = None
    synced_hash: NonEmpty | None = None


class NodeLine(_Line):
    """One line of graph.ndjson, as it may be written by hand."""

    id: Id
    parent: Id | None = None
    title: Annotated[str, AfterValidator(_check_title)]
    kind: Kind | None = None
    status: Status | None = None
    depends_on: list[Id] | None = None
    delivery: DeliveryLine | None = None
    external: ExternalLine | None = None
    variant_of: Id | None = None

    def to_node(self) -> Node:
        """The canonical node: defaults filled, nulls and empties dropped, deps sorted."""
        delivery = external = None
        if (d := self.delivery) is not None:
            delivery = Delivery(
                repo=d.repo, branch=d.branch, commits=tuple(d.commits or ()), pr=d.pr
            )
        if (e := self.external) is not None:
            external = External(
                provider=e.provider, id=e.id, synced_at=e.synced_at, synced_hash=e.synced_hash
            )
        return Node(
            id=self.id,
            parent=self.parent,
            title=self.title,
            kind=self.kind or Kind.WORK,
            status=self.status or Status.TODO,
            depends_on=canonical_ids(self.depends_on or ()),
            delivery=delivery,
            external=external,
            variant_of=self.variant_of,
        )


def parse_graph(text: str, source: str = "graph.ndjson") -> list[Node]:
    """Parses graph.ndjson text. Returns nodes in canonical form, sorted by id. Raises
    `GraphParseError` listing every bad line.
    """
    issues: list[ParseIssue] = []
    first_seen: dict[NodeId, int] = {}
    nodes: list[Node] = []
    # Some editors start files with a byte-order mark; it isn't part of line 1's JSON.
    for number, raw in enumerate(text.removeprefix("﻿").split("\n"), start=1):
        content = raw.removesuffix("\r")
        if not content.strip():
            continue
        try:
            node = NodeLine.model_validate_json(content).to_node()
        except ValidationError as err:
            issues.extend(ParseIssue(number, _line_issue(e)) for e in err.errors())
            continue
        if (previous := first_seen.get(node.id)) is not None:
            issues.append(ParseIssue(number, f"duplicate id {node.id} (first on line {previous})"))
            continue
        first_seen[node.id] = number
        nodes.append(node)
    if issues:
        raise GraphParseError(issues, source)
    return sorted(nodes, key=lambda n: n.id)


def canonical_ids(ids: Iterable[NodeId]) -> tuple[NodeId, ...]:
    """Sorted, de-duplicated. UUIDv7s sort by creation time."""
    return tuple(sorted(set(ids)))


def _line_issue(error: ErrorDetails) -> str:
    if error["type"] == "json_invalid":
        return f"invalid JSON: {error['msg'].removeprefix('Invalid JSON: ')}"
    if error["type"] == "model_type" and not error["loc"]:
        return "expected a JSON object"
    return format_issue(error)
