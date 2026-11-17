from pathlib import Path

from apm.core.body import read_body, render_body, write_body

ID = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"


def test_reads_a_missing_body_as_empty(tmp_path: Path) -> None:
    assert read_body(tmp_path, ID) == ""


def test_round_trips_a_body_under_nodes(tmp_path: Path) -> None:
    write_body(tmp_path, ID, "## Intent\n\nhello\n")
    assert read_body(tmp_path, ID) == "## Intent\n\nhello\n"
    assert [p.name for p in (tmp_path / "nodes").iterdir()] == [f"{ID}.md"]


def test_renders_the_empty_template() -> None:
    assert render_body() == "## Intent\n\n## Acceptance criteria\n\n## Notes\n"


def test_fills_intent_acceptance_criteria_and_notes() -> None:
    body = render_body(
        intent="  Show agent output live.\n",
        acceptance=["Events appear within 1s", " Survives reconnect "],
        notes="See §7.1.",
    )
    assert body == (
        "## Intent\n\nShow agent output live.\n\n"
        "## Acceptance criteria\n\n- [ ] Events appear within 1s\n- [ ] Survives reconnect\n\n"
        "## Notes\n\nSee §7.1.\n"
    )
