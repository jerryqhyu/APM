"""`costs.ndjson`: the frozen cost of each node whose work has merged, next to the plan.

One line per node, sorted by node id, keys in a fixed order. A record is written once: frozen
means frozen, so a second write for the same node is refused. Writes go through the write lock
and an atomic rename, like graph.ndjson.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from apm.core.errors import ApmError
from apm.core.files import read_file_if_exists, write_file_atomic
from apm.core.lock import acquire_write_lock
from apm.core.model import NodeId

COSTS_FILE = "costs.ndjson"


class CostFrozenError(ApmError):
    """The node's cost is already frozen."""


@dataclass(frozen=True, slots=True)
class CostRecord:
    node_id: NodeId
    total: float
    """USD."""
    by_agent: Mapping[str, float]
    """USD per agent type."""
    frozen_at: str
    """ISO timestamp."""
    merged: str
    """The pull request URL, or the branch, whose merge froze it."""


def costs_path(apm_dir: Path) -> Path:
    return apm_dir / COSTS_FILE


def read_costs(apm_dir: Path) -> dict[NodeId, CostRecord]:
    """Every frozen cost by node id; none if the file is missing."""
    text = read_file_if_exists(costs_path(apm_dir)) or ""
    records = (json.loads(line) for line in text.splitlines() if line.strip())
    return {r["node_id"]: CostRecord(**r) for r in records}


def freeze_cost(apm_dir: Path, record: CostRecord) -> None:
    """Adds `record`. Raises `CostFrozenError` if its node already has one."""
    with acquire_write_lock(apm_dir):
        costs = read_costs(apm_dir)
        if record.node_id in costs:
            raise CostFrozenError(f"the cost of {record.node_id} is already frozen")
        costs[record.node_id] = record
        write_file_atomic(costs_path(apm_dir), "".join(_line(costs[n]) for n in sorted(costs)))


def _line(r: CostRecord) -> str:
    fields = {
        "node_id": r.node_id,
        "total": r.total,
        "by_agent": dict(sorted(r.by_agent.items())),
        "frozen_at": r.frozen_at,
        "merged": r.merged,
    }
    return json.dumps(fields, ensure_ascii=False) + "\n"
