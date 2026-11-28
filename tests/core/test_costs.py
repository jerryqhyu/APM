from pathlib import Path

import pytest

from apm.core.costs import (
    CostFrozenError,
    CostRecord,
    CostView,
    cost_line,
    costs_path,
    freeze_cost,
    live_cost,
    node_cost_view,
    read_costs,
)
from apm.core.graph import Graph
from apm.core.store import NodeCost, Store
from tests.core.build import make_id, node

A = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"
B = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a45"
F, T1, T2, S, T3 = (make_id(n) for n in range(1, 6))


def record(node_id: str = A, total: float = 1.25) -> CostRecord:
    return CostRecord(
        node_id=node_id,
        total=total,
        by_agent={"apm-implementer": 1.0, "apm-task-reviewer": 0.25},
        frozen_at="2026-11-27T12:00:00+00:00",
        merged="https://github.com/o/r/pull/7",
    )


def test_a_frozen_cost_round_trips_every_field(tmp_path: Path) -> None:
    freeze_cost(tmp_path, record(B))
    freeze_cost(tmp_path, record(A))
    assert read_costs(tmp_path) == {A: record(A), B: record(B)}
    lines = costs_path(tmp_path).read_text().splitlines()
    assert [A in lines[0], B in lines[1]] == [True, True]  # sorted by node id
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_freezing_a_node_twice_is_refused(tmp_path: Path) -> None:
    freeze_cost(tmp_path, record(A))
    before = costs_path(tmp_path).read_bytes()
    with pytest.raises(CostFrozenError, match="already frozen"):
        freeze_cost(tmp_path, record(A, total=9.0))
    assert costs_path(tmp_path).read_bytes() == before


def test_a_missing_file_reads_as_no_records(tmp_path: Path) -> None:
    assert read_costs(tmp_path) == {}


def _graph_and_store(tmp_path: Path) -> tuple[Graph, Store]:
    # F (feature) > T1 (frozen task), T2 (live task), S (sub-feature) > T3 (live task)
    graph = Graph([node(F), node(T1, F), node(T2, F), node(S, F), node(T3, S)])
    store = Store(tmp_path)
    for node_id, agent, cost in [
        (F, "apm-feature-reviewer", 1.0),
        (F, "apm-eager-builder", 2.0),
        (T1, "apm-implementer", 100.0),  # ignored once T1 is frozen
        (T2, "apm-implementer", 0.5),
        (T2, "apm-critic", 0.25),
        (S, "apm-feature-reporter", 0.125),
        (T3, "apm-implementer", 4.0),
    ]:
        run = store.create_run(node_id=node_id, agent=agent)
        store.finish_run(run.id, "succeeded", cost_usd=cost)
    return graph, store


def test_a_feature_totals_its_runs_and_its_frozen_and_live_tasks(tmp_path: Path) -> None:
    graph, store = _graph_and_store(tmp_path)
    frozen = {T1: record(T1)}  # implementer 1.0, task reviewer 0.25
    assert live_cost(graph, store, frozen, F) == NodeCost(
        total=9.125,
        by_agent={
            "apm-critic": 0.25,
            "apm-eager-builder": 2.0,
            "apm-feature-reporter": 0.125,
            "apm-feature-reviewer": 1.0,
            "apm-implementer": 5.5,
            "apm-task-reviewer": 0.25,
        },
    )


def test_a_sub_feature_counts_once_in_its_parent(tmp_path: Path) -> None:
    graph, store = _graph_and_store(tmp_path)
    sub = live_cost(graph, store, {}, S)
    assert sub == NodeCost(
        total=4.125, by_agent={"apm-feature-reporter": 0.125, "apm-implementer": 4.0}
    )
    whole = live_cost(graph, store, {}, F).total
    assert whole == 1.0 + 2.0 + 100.0 + 0.75 + sub.total
    frozen_sub = CostRecord(S, 7.0, {"apm-implementer": 7.0}, "2026-11-27T12:00:00+00:00", "b")
    assert live_cost(graph, store, {S: frozen_sub}, F).total == whole - sub.total + 7.0


def test_a_frozen_task_shows_its_frozen_total_and_the_merge_it_came_from(tmp_path: Path) -> None:
    graph, _ = _graph_and_store(tmp_path)
    freeze_cost(tmp_path, record(T1))
    view = node_cost_view(tmp_path, graph, T1)
    assert view == CostView(
        1.25,
        {"apm-implementer": 1.0, "apm-task-reviewer": 0.25},
        frozen=True,
        merged="https://github.com/o/r/pull/7",
    )
    assert cost_line(view) == (
        "cost: $1.25 frozen (merged https://github.com/o/r/pull/7)"
        " · apm-implementer $1.00, apm-task-reviewer $0.25"
    )


def test_a_live_feature_shows_its_running_total_by_agent(tmp_path: Path) -> None:
    graph, _ = _graph_and_store(tmp_path)
    view = node_cost_view(tmp_path, graph, S)
    assert view == CostView(
        4.125, {"apm-feature-reporter": 0.125, "apm-implementer": 4.0}, frozen=False, merged=None
    )
    assert cost_line(view) == "cost: $4.12 live · apm-feature-reporter $0.12, apm-implementer $4.00"
    assert cost_line(node_cost_view(tmp_path, Graph([node(A)]), A)) == "cost: $0.00 live"
