"""`costs.ndjson`: the frozen cost of each node whose work has merged, next to the plan.

One line per node, sorted by node id, keys in a fixed order. A record is written once: frozen
means frozen, so a second write for the same node is refused. Writes go through the write lock
and an atomic rename, like graph.ndjson.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from apm.core.config import ApmConfig
from apm.core.errors import ApmError
from apm.core.files import read_file_if_exists, write_file_atomic
from apm.core.graph import Graph
from apm.core.lock import acquire_write_lock
from apm.core.model import NodeId
from apm.core.settings import resolve_settings
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


def over_budget(
    config: ApmConfig,
    graph: Graph,
    store: Store,
    frozen: Mapping[NodeId, CostRecord],
    node_id: NodeId,
) -> tuple[NodeId, float] | None:
    """The feature whose budget `node_id` falls under, and that budget, if its live cost has
    reached it. The project's default budget applies to the node's top-level feature.
    """
    resolved = resolve_settings(config, graph, node_id)
    if (budget := resolved.settings.budget_usd) is None:
        return None
    owner = resolved.sources["budget_usd"] or [node_id, *graph.ancestors(node_id)][-1]
    return (owner, budget) if live_cost(graph, store, frozen, owner).total >= budget else None


@dataclass(frozen=True, slots=True)
class PlanCosts:
    total: float
    """USD: every run's cost, live."""
    by_feature: dict[NodeId, float]
    """Each feature's `live_cost` total, most costly first."""
    by_agent: dict[str, float]
    by_day: dict[str, float]
    """By local day (YYYY-MM-DD), ascending."""


def plan_costs(graph: Graph, store: Store, frozen: Mapping[NodeId, CostRecord]) -> PlanCosts:
    """The whole plan's cost: the total, per feature, per agent and per day."""
    by_agent, by_day = store.cost_breakdown()
    features = {f for n in graph.nodes if (f := graph.feature_of(n)) is not None}
    by_feature = {f: live_cost(graph, store, frozen, f).total for f in sorted(features)}
    return PlanCosts(
        total=sum(by_agent.values()),
        by_feature=dict(sorted(by_feature.items(), key=lambda kv: -kv[1])),
        by_agent=by_agent,
        by_day=by_day,
    )


def _line(r: CostRecord) -> str:
    fields = {
        "node_id": r.node_id,
        "total": r.total,
        "by_agent": dict(sorted(r.by_agent.items())),
        "frozen_at": r.frozen_at,
        "merged": r.merged,
    }
    return json.dumps(fields, ensure_ascii=False) + "\n"
