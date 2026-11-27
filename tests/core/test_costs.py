from pathlib import Path

import pytest

from apm.core.costs import CostFrozenError, CostRecord, costs_path, freeze_cost, read_costs

A = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"
B = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a45"


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
