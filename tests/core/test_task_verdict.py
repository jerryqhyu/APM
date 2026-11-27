"""The task reviewer's verdict: exactly one of pass, too_big or unclear, strictly shaped."""

from typing import Any

import pytest

from apm.core.task_verdict import TASK_VERDICT_SCHEMA, VerdictRefused, parse_task_verdict


def child(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "title": f"Part {key}",
        "kind": "work",
        "intent": "",
        "context": "",
        "approach": "",
        "acceptance": ["works"],
        "out_of_scope": [],
        "depends_on_keys": [],
        "depends_on_ids": [],
    }


def answer(verdict: str, **fields: Any) -> dict[str, Any]:
    return {"verdict": verdict, "summary": "why", "children": [], "missing": []} | fields


def test_it_accepts_each_verdict() -> None:
    assert parse_task_verdict(answer("pass")).verdict == "pass"
    split = parse_task_verdict(answer("too_big", children=[child("a"), child("b")]))
    assert [c.title for c in split.children] == ["Part a", "Part b"]
    unclear = parse_task_verdict(answer("unclear", missing=["which file to change"]))
    assert unclear.missing == ["which file to change"]
    assert parse_task_verdict({"verdict": "pass"}).summary == ""  # the rest is optional


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (answer("maybe"), "verdict: Input should be 'pass', 'too_big' or 'unclear'"),
        (answer("too_big"), "too_big needs at least two proposed child tasks"),
        (answer("too_big", children=[child("a")]), "at least two proposed child tasks"),
        (answer("unclear"), "unclear needs a non-empty list of what is missing"),
        (answer("unclear", missing=[""]), "missing[0]: String should have at least 1"),
        (answer("pass", score=3), "score: Extra inputs are not permitted"),
        (
            answer("too_big", children=[child("a"), child("b") | {"size": 9}]),
            "children[1].size: Extra inputs are not permitted",
        ),
        ([], "Input should be a valid dictionary"),
    ],
)
def test_it_rejects_anything_else(data: object, problem: str) -> None:
    with pytest.raises(VerdictRefused, match="not a valid task verdict") as err:
        parse_task_verdict(data)
    assert problem in str(err.value)


def test_the_schema_matches_the_model() -> None:
    assert TASK_VERDICT_SCHEMA["additionalProperties"] is False
    assert TASK_VERDICT_SCHEMA["properties"]["verdict"]["enum"] == ["pass", "too_big", "unclear"]
    assert set(TASK_VERDICT_SCHEMA["required"]) == set(answer("pass"))
    items = TASK_VERDICT_SCHEMA["properties"]["children"]["items"]
    assert set(items["required"]) == set(child("a"))
