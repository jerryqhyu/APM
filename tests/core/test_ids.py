import pytest
from hypothesis import given
from hypothesis import strategies as st

from apm.core.ids import HandleError, handle, handles, is_node_id, new_id, resolve_handle
from tests.strategies import node_ids


def test_new_ids_are_uuid7s_in_creation_order_even_within_one_millisecond() -> None:
    ids = [new_id() for _ in range(2000)]
    assert all(is_node_id(i) for i in ids)
    assert sorted(ids) == ids
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44", True),
        ("0192F3A1-7C4E-7B91-A2D5-3F8E1C0B9A44", False),  # stored IDs are lowercase
        ("0192f3a1-7c4e-4b91-a2d5-3f8e1c0b9a44", False),  # v4
        ("0192f3a1-7c4e-7b91-c2d5-3f8e1c0b9a44", False),  # wrong variant
        ("0192f3a17c4e7b91a2d53f8e1c0b9a44", False),
        ("../../etc/passwd", False),
    ],
)
def test_is_node_id(value: str, ok: bool) -> None:
    assert is_node_id(value) is ok


def test_handle_is_the_last_8_hex_chars() -> None:
    assert handle("0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44") == "1c0b9a44"


def test_handles_lengthens_only_the_handles_that_collide() -> None:
    a = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"
    b = "0192f3a2-0000-7000-8000-00001c0b9a44"  # same last 8 as a
    c = "0192f3a3-0000-7000-8000-000000000001"
    assert handles([a, b, c]) == {a: "e1c0b9a44", b: "01c0b9a44", c: "00000001"}


@given(st.lists(node_ids(), max_size=30, unique=True))
def test_every_displayed_handle_is_unique_and_resolves_back(ids: list[str]) -> None:
    shown = handles(ids)
    assert len(set(shown.values())) == len(ids)
    for node_id in ids:
        assert resolve_handle(shown[node_id], ids) == node_id


IDS = [
    "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44",
    "0192f3a1-7c4e-7b91-a2d5-0000aaaa1234",
    "0192f3a1-7c4e-7b91-a2d5-0000bbbb1234",
]


def test_resolve_accepts_the_handle_a_shorter_unique_suffix_or_the_full_id() -> None:
    assert resolve_handle("1c0b9a44", IDS) == IDS[0]
    assert resolve_handle("9a44", IDS) == IDS[0]
    assert resolve_handle("aaaa1234", IDS) == IDS[1]
    assert resolve_handle(IDS[0], IDS) == IDS[0]


def test_resolve_ignores_case_hyphens_and_surrounding_whitespace() -> None:
    assert resolve_handle(" 1C0B9A44 ", IDS) == IDS[0]
    assert resolve_handle("a2d5-3f8e1c0b9a44", IDS) == IDS[0]
    assert resolve_handle("0192F3A17C4E7B91A2D53F8E1C0B9A44", IDS) == IDS[0]


def test_resolve_reports_ambiguity_with_every_candidate() -> None:
    with pytest.raises(HandleError) as caught:
        resolve_handle("1234", IDS)
    assert caught.value.code == "ambiguous"
    assert caught.value.candidates == (IDS[1], IDS[2])
    assert "aaaa1234" in str(caught.value)
    assert "bbbb1234" in str(caught.value)


def test_resolve_never_matches_the_leading_timestamp_chars() -> None:
    # These share their first 8 chars, as nodes created close together do.
    with pytest.raises(HandleError) as caught:
        resolve_handle("0192f3a1", IDS)
    assert caught.value.code == "unknown"


@pytest.mark.parametrize("value", ["", "abc", "xyz12345", "1c0b 9a44", "0" * 33])
def test_resolve_rejects_invalid_input(value: str) -> None:
    with pytest.raises(HandleError) as caught:
        resolve_handle(value, IDS)
    assert caught.value.code == "invalid"


@pytest.mark.parametrize("ids", [IDS, []])
def test_resolve_reports_unknown_handles(ids: list[str]) -> None:
    with pytest.raises(HandleError) as caught:
        resolve_handle("deadbeef", ids)
    assert caught.value.code == "unknown"
