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
from apm.core.graph import Graph
from apm.core.lock import acquire_write_lock
from apm.core.model import NodeId
from apm.core.store import NodeCost, Store

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


def live_cost(
    graph: Graph, store: Store, frozen: Mapping[NodeId, CostRecord], node_id: NodeId
) -> NodeCost:
    """What `node_id` has cost, by agent: its frozen figure if it has one, else its own runs
    plus each child's cost, worked out the same way. Each node counts once, through its parent.
    """
    if (record := frozen.get(node_id)) is not None:
        return NodeCost(total=record.total, by_agent=dict(record.by_agent))
    by_agent = dict(store.node_cost(node_id).by_agent)
    for child in graph.children(node_id):
        for agent, cost in live_cost(graph, store, frozen, child).by_agent.items():
            by_agent[agent] = by_agent.get(agent, 0.0) + cost
    return NodeCost(total=sum(by_agent.values()), by_agent=dict(sorted(by_agent.items())))


def _line(r: CostRecord) -> str:
    fields = {
        "node_id": r.node_id,
        "total": r.total,
        "by_agent": dict(sorted(r.by_agent.items())),
        "frozen_at": r.frozen_at,
        "merged": r.merged,
    }
    return json.dumps(fields, ensure_ascii=False) + "\n"
