"""Editing apm.yaml in place: only what was asked changes, and comments and layout stay."""

import pytest
import yaml

from apm.core import config_edit
from apm.core.config_edit import ConfigEditError, edit_yaml

ID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000"

# The settings example from the user guide: flow mappings with spaces, aligned comments.
GUIDE = """\
name: my-app
code:
  repos:
    my-app: { path: "..", default: true }
runner:
  max_concurrent: 2          # agents working at once
  default_budget_usd: 2.00   # spending cap for one run
  agents:
    apm-implementer:
      model: opus
      allowed_tools: ["Bash(uv run *)", "Bash(npm test*)"]
automation:                  # what `apm serve` does on its own (see below)
  implement_follow_ups: no
"""

# Section headings, a commented-out setting and a comment at the very end.
HEADINGS = """\
name: my-app
code:
  repos:
    my-app:
      path: ..
      allowed:
        - a
        - b

# The features
features:
  eager: false
  # task_max_lines: 80

# What apm serve does
automation:
  task_review: true
# the end
"""


@pytest.mark.parametrize("text", [GUIDE, HEADINGS], ids=["guide", "headings"])
def test_no_change_gives_the_same_text(text: str) -> None:
    assert edit_yaml(text, {}) == text


def test_setting_values_keeps_everything_else_as_written() -> None:
    edited = edit_yaml(
        GUIDE, {("automation", "task_review"): False, ("runner", "max_concurrent"): 4}
    )
    assert edited == GUIDE.replace(
        "max_concurrent: 2          #", "max_concurrent: 4          #"
    ).replace("  implement_follow_ups: no\n", "  implement_follow_ups: no\n  task_review: false\n")


def test_a_new_section_goes_at_the_end_with_the_files_indentation() -> None:
    four = "name: x\ncode:\n    repos:\n        app:\n            path: ..\n"
    edited = edit_yaml(four, {("features", "overrides", ID, "eager"): True})
    assert edited == f"{four}features:\n    overrides:\n        {ID}:\n            eager: true\n"


def test_added_entries_go_above_the_comments_that_followed() -> None:
    edited = edit_yaml(
        HEADINGS,
        {
            ("features", "report"): False,
            ("features", "overrides", ID, "report_detail"): "detailed",
            ("automation", "feature_pr"): False,
        },
    )
    assert edited == HEADINGS.replace(
        "  eager: false\n",
        f"  eager: false\n  report: false\n  overrides:\n"
        f"    {ID}:\n      report_detail: detailed\n",
    ).replace("  task_review: true\n", "  task_review: true\n  feature_pr: false\n")


def test_a_plain_value_goes_above_the_nested_mappings() -> None:
    text = f"name: x\nfeatures:\n  eager: true\n  overrides:\n    {ID}:\n      report: false\n"
    edited = edit_yaml(text, {("features", "task_max_lines"): 80})
    assert edited == text.replace("  eager: true\n", "  eager: true\n  task_max_lines: 80\n")
    only_overrides = f"name: x\nfeatures:\n  overrides:\n    {ID}:\n      report: false\n"
    edited = edit_yaml(only_overrides, {("features", "eager"): True})
    assert edited == only_overrides.replace("features:\n", "features:\n  eager: true\n")


def test_removing_the_last_entries_removes_their_empty_mappings_and_keeps_the_comments() -> None:
    added = edit_yaml(HEADINGS, {("features", "overrides", ID, "eager"): True})
    assert edit_yaml(added, {("features", "overrides", ID, "eager"): None}) == HEADINGS


def test_removing_an_entry_keeps_the_comment_above_the_next_one() -> None:
    text = (
        "name: x\nfeatures:\n  overrides:\n    01a1fe93:\n      eager: true\n"
        "    # the next one matters\n    02b2fe93:\n      eager: false\n"
    )
    edited = edit_yaml(text, {("features", "overrides", "01a1fe93", "eager"): None})
    assert edited == (
        "name: x\nfeatures:\n  overrides:\n    # the next one matters\n    02b2fe93:\n"
        "      eager: false\n"
    )


def test_flow_mappings_stay_flow_and_keep_their_spacing() -> None:
    text = "name: x\nfeatures:\n  overrides:\n    01a1fe93: { eager: true }\n"
    edited = edit_yaml(text, {("features", "overrides", "01a1fe93", "report_detail"): "brief"})
    assert edited == text.replace("{ eager: true }", "{ eager: true, report_detail: brief }")
    tight = "name: x\nautomation: {feature_pr: false}\n"
    assert edit_yaml(tight, {("automation", "reconcile"): False}) == (
        "name: x\nautomation: {feature_pr: false, reconcile: false}\n"
    )


def test_values_are_written_as_what_they_are() -> None:
    edited = edit_yaml(
        "name: x\n",
        {
            ("features", "report_detail"): "true",
            ("features", "task_max_lines"): 80,
            ("automation", "reconcile_interval_s"): 60.0,
        },
    )
    assert yaml.safe_load(edited) == {
        "name": "x",
        "features": {"report_detail": "true", "task_max_lines": 80},
        "automation": {"reconcile_interval_s": 60.0},
    }


def test_a_value_the_file_already_has_is_left_as_spelled() -> None:
    text = "name: x\nautomation:\n  reconcile: yes\n  reconcile_interval_s: 300\n"
    same = edit_yaml(
        text, {("automation", "reconcile"): True, ("automation", "reconcile_interval_s"): 300.0}
    )
    assert same == text
    assert "reconcile: false" in edit_yaml(text, {("automation", "reconcile"): False})


def test_values_are_left_as_spelled_even_where_yaml_versions_disagree() -> None:
    # PyYAML reads `no` as false and `12e45678` as text; a YAML 1.2 reader wouldn't.
    text = "name: x\nautomation:\n  reconcile: no\nfeatures:\n  overrides:\n    12e45678: {}\n"
    edited = edit_yaml(text, {("features", "eager"): True})
    assert "reconcile: no\n" in edited
    assert "12e45678:" in edited


def test_an_empty_section_becomes_a_mapping() -> None:
    text = "name: x\nfeatures:\n  # eager: true\nautomation:\n  reconcile: true\n"
    edited = edit_yaml(text, {("features", "eager"): True})
    assert yaml.safe_load(edited)["features"] == {"eager": True}
    assert "# eager: true" in edited


def test_a_section_that_is_no_mapping_is_refused() -> None:
    with pytest.raises(ConfigEditError, match=r"features in apm\.yaml must be a mapping"):
        edit_yaml("name: x\nfeatures: 3\n", {("features", "eager"): True})


def test_removing_what_is_not_there_changes_nothing() -> None:
    text = "name: x\nfeatures:\n  overrides: {}\n"
    assert edit_yaml(text, {("features", "overrides", ID, "eager"): None}) == text
    assert edit_yaml(text, {("automation", "reconcile"): None}) == text


def test_unreadable_yaml_is_refused() -> None:
    with pytest.raises(ConfigEditError, match="can't be read"):
        edit_yaml("name: [\n", {("features", "eager"): True})
    with pytest.raises(ConfigEditError, match=r"can't be read: 2:1: found duplicate key \"name\""):
        edit_yaml("name: x\nname: y\n", {("features", "eager"): True})


def test_a_result_that_means_more_than_asked_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stands in for a quirk of the round trip: the value written isn't the value asked for.
    monkeypatch.setattr(config_edit, "_scalar", lambda value: "maybe")
    with pytest.raises(ConfigEditError, match="without touching other settings"):
        edit_yaml("name: x\n", {("features", "eager"): True})
