"""`apm settings`, `apm review-tasks` and `apm implement`."""

import pytest

from apm.core.git import log
from apm.core.ids import handle
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.store import Store
from tests.runner.fake import FakeClaude
from tests.runner.test_runner import Harness
from tests.test_cli import apm


@pytest.fixture
def cwd(project: Project, monkeypatch: pytest.MonkeyPatch) -> Project:
    monkeypatch.chdir(project.code_dir)
    return project


def feature(project: Project, *titles: str) -> tuple[str, list[str]]:
    top = create_nodes(project, [NewNode(title="Search")]).created[0]
    kids = create_nodes(project, [NewNode(title=t, parent=top) for t in titles]).created
    return top, list(kids)


def test_settings_show_set_and_clear(cwd: Project) -> None:
    top, (task,) = feature(cwd, "Index")
    shown = apm("settings", "show")
    assert "  eager                  off\n" in shown
    assert "  task_max_lines         50\n" in shown
    assert "  eager_redo             on\n" in shown

    said = apm("settings", "set", "eager=on", "task_max_lines=80", "automation.reconcile=off")
    assert said.strip() == (
        "settings: eager mode on, task size limit 80 lines, automation reconcile off"
    )
    assert apm("settings", "set", "eager=yes").strip() == "nothing to change"

    said = apm("settings", "set", "--node", handle(top), "task_max_lines=30", "report=off")
    assert said.strip() == (
        f'settings {handle(top)}: task size limit 30 lines, feature report off for "Search"'
    )
    shown = apm("settings", "show", handle(task))
    assert f"  task_max_lines   30         from {handle(top)} Search\n" in shown
    assert "  eager            on         the project\n" in shown
    assert f"  {handle(top)} Search: task_max_lines 30, report off\n" in apm("settings", "show")
    assert "set here" in apm("settings", "show", handle(top))

    assert "own settings cleared" in apm("settings", "clear", handle(top))
    assert apm("settings", "clear", handle(top)).strip() == "nothing to clear"
    assert apm("settings", "set", "eager=default").strip() == "settings: eager mode back to default"


def test_settings_refuses_what_it_cannot_set(cwd: Project) -> None:
    commits = len(log(cwd.apm_dir))
    assert "features.task_max_lines" in apm("settings", "set", "task_max_lines=2", code=1)
    assert "expected NAME=VALUE" in apm("settings", "set", "eager", code=1)
    assert "unknown setting features.nope" in apm("settings", "set", "nope=1", code=1)
    assert "needs a value" in apm("settings", "set", "eager=", code=1)
    top, _ = feature(cwd, "Index")
    refused = apm("settings", "set", "-n", handle(top), "automation.reconcile=off", code=1)
    assert "automation switches are project-wide" in refused
    assert len(log(cwd.apm_dir)) == commits + 2  # the two nodes of `feature`


def test_check_reports_overrides_that_apply_to_nothing(cwd: Project) -> None:
    config = cwd.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + "features:\n  overrides:\n    abcdef12:\n")
    assert apm("check").strip() == "ok"  # an empty key is not set, like any other
    config.write_text(config.read_text().replace("abcdef12:", "abcdef12: { eager: true }"))
    assert "warning apm.yaml: features.overrides.abcdef12 matches no node" in apm("check")
    config.write_text(config.read_text().replace("eager: true", "eager: maybe"))
    assert "error apm.yaml: features.overrides.abcdef12.eager" in apm("check", code=1)


def test_review_tasks_reviews_every_task_and_queues_beyond_the_limit(
    cwd: Project, fake_claude: FakeClaude
) -> None:
    config = cwd.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + "runner:\n  max_concurrent: 1\n")
    top, tasks = feature(cwd, "Parse", "Index")
    output = apm("review-tasks", handle(top))
    graph = load_graph(cwd)
    assert f"started 1: {graph.handles[tasks[0]]} Parse\n" in output
    assert f"waiting for room under runner.max_concurrent, 1: {graph.handles[tasks[1]]}" in output
    assert f"[{handle(tasks[1])}] ▶ apm-task-reviewer" in output
    runs = Store(cwd.apm_dir).list_runs()
    assert sorted(r.node_id for r in runs) == sorted(tasks)
    assert {r.status for r in runs} == {"succeeded"}


def test_implement_builds_what_is_ready_and_says_what_it_skipped(
    cwd: Project, fake_claude: FakeClaude
) -> None:
    top, (ready, unreviewed) = feature(cwd, "Parse", "Index")
    Harness(cwd).review(ready)
    output = apm("implement", handle(top), "-m", "Keep it short.")
    assert f"started 1: {handle(ready)} Parse\n" in output
    assert f"skipped {handle(unreviewed)} Index: this task's current content has not been" in output
    (call,) = fake_claude.calls
    assert call["agent"] == "apm-implementer"
    assert "Keep it short." in call["prompt"]


def test_implement_with_nothing_to_do_says_so(cwd: Project) -> None:
    top, (task,) = feature(cwd, "Parse")
    apm("node", "status", handle(task), "done")
    assert apm("implement", handle(top)).strip() == "nothing to implement: it has no To do tasks"
    assert apm("review-tasks", handle(top)).strip() == "nothing to review: it has no To do tasks"


def test_a_failed_run_fails_the_command(cwd: Project, fake_claude: FakeClaude) -> None:
    top, _ = feature(cwd, "Parse")
    fake_claude.mode("fail")
    assert "✗ failed" in apm("review-tasks", handle(top), code=1)
