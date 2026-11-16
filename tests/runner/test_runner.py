import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from apm.core.ids import handle
from apm.core.model import Status
from apm.core.ops import NewNode, create_nodes, set_status
from apm.core.project import Project
from apm.core.store import Store
from apm.runner.agents import RunRefused
from apm.runner.runner import Runner, RunnerBusy
from tests.runner.fake import FakeClaude


class Harness:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.store = Store(project.apm_dir)
        self.events: list[dict[str, Any]] = []
        self.runner = Runner(project, self.store, self.events.append)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


@pytest.fixture
def harness(project: Project, fake_claude: FakeClaude) -> Harness:
    return Harness(project)


@pytest.fixture
def node(project: Project) -> str:
    return create_nodes(project, [NewNode(title="File I/O")]).created[0]


def option(call: dict, name: str) -> str:
    argv = call["argv"]
    return argv[argv.index(name) + 1]


async def test_a_decomposer_run_becomes_a_reviewed_proposal(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    run = harness.runner.start("apm-decomposer", node, "Keep it small.")
    assert run.status == "running"
    await harness.runner.wait(node)

    decomposer, critic = sorted(harness.store.list_runs(), key=lambda r: r.started)
    assert (decomposer.id, decomposer.agent, decomposer.status) == (
        run.id,
        "apm-decomposer",
        "succeeded",
    )
    assert (critic.agent, critic.status, critic.parent_run) == ("apm-critic", "succeeded", run.id)
    assert decomposer.cost_usd == 0.25

    (proposal,) = harness.store.list_proposals()
    assert (proposal.node_id, proposal.run_id, proposal.status) == (node, run.id, "pending")
    assert [c["title"] for c in proposal.data["children"]] == ["Parse", "Write"]
    assert proposal.review is not None
    assert proposal.review["verdict"] == "accept"
    assert harness.types() == [
        "run.started",
        "run.event",
        "run.event",
        "run.event",
        "run.finished",
        "run.started",
        "run.event",
        "run.event",
        "run.event",
        "run.finished",
        "proposal.created",
    ]
    assert harness.runner.active_nodes() == []

    # Only the events a transcript needs are stored: noise and non-JSON lines are dropped.
    stored = [e.data["type"] for e in harness.store.list_events(run.id)]
    assert stored == ["system", "assistant", "result"]

    first, second = fake_claude.calls
    assert first["cwd"] == str(harness.project.code_dir)
    assert option(first, "--agent") == "apm:apm-decomposer"
    assert option(first, "-p").startswith(f'Decompose node {handle(node)} ("File I/O")')
    assert "Keep it small." in option(first, "-p")
    assert option(first, "--session-id") == decomposer.session_id
    assert option(first, "--permission-prompts") == "none"
    assert option(first, "--max-budget-usd") == "2"
    assert "--strict-mcp-config" in first["argv"]
    assert json.loads(option(first, "--json-schema"))["required"] == [
        "rationale",
        "children",
        "warnings",
    ]
    allowed = option(first, "--allowedTools").split(",")
    assert "mcp__apm__apm_get_graph" in allowed
    assert "Read" in allowed
    assert not any("set_status" in tool or tool in ("Edit", "Write") for tool in allowed)
    assert option(second, "--agent") == "apm:apm-critic"
    assert '"title": "Parse"' in option(second, "-p")

    mcp = json.loads(Path(option(first, "--mcp-config")).read_text())["mcpServers"]["apm"]
    assert mcp["args"] == ["-m", "apm", "mcp"]
    assert mcp["env"] == {
        "APM_PROJECT": str(harness.project.code_dir),
        "APM_RUN_SCOPE": "read",
        "APM_ACTOR": "agent:apm-decomposer",
        "APM_RUN": run.id,
    }


async def test_a_revise_verdict_gets_one_revision(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("revise")
    run = harness.runner.start("apm-decomposer", node)
    await harness.runner.wait(node)

    runs = sorted(harness.store.list_runs(), key=lambda r: r.started)
    assert [r.agent for r in runs] == ["apm-decomposer", "apm-critic", "apm-decomposer"]
    revision = runs[2]
    assert revision.parent_run == run.id
    assert revision.session_id == runs[0].session_id
    (proposal,) = harness.store.list_proposals()
    assert proposal.run_id == revision.id
    assert proposal.data["rationale"] == "Revised."
    assert proposal.review is not None
    assert (proposal.review["verdict"], proposal.review["revised"]) == ("revise", True)

    third = fake_claude.calls[2]
    assert option(third, "--resume") == runs[0].session_id
    assert "--session-id" not in third["argv"]
    assert "Too big." in option(third, "-p")


async def test_the_critic_can_be_skipped_or_fail_without_losing_the_proposal(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    harness.runner.start("apm-decomposer", node, critic=False)
    await harness.runner.wait(node)
    assert [r.agent for r in harness.store.list_runs()] == ["apm-decomposer"]
    assert harness.store.list_proposals()[0].review is None

    fake_claude.mode("critic-fails")
    harness.runner.start("apm-decomposer", node)
    await harness.runner.wait(node)
    critic = next(r for r in harness.store.list_runs() if r.agent == "apm-critic")
    assert critic.status == "failed"
    assert len(harness.store.list_proposals()) == 2
    assert harness.store.list_proposals()[0].review is None


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("fail", "claude exited with code 1: boom: something broke"),
        ("error-result", "error_max_budget_usd: over budget"),
        ("no-answer", "the agent returned no structured answer"),
        ("bad-proposal", "the decomposition has no children"),
    ],
)
async def test_a_failed_run_records_why_and_makes_no_proposal(
    harness: Harness, node: str, fake_claude: FakeClaude, mode: str, error: str
) -> None:
    fake_claude.mode(mode)
    run = harness.runner.start("apm-decomposer", node)
    await harness.runner.wait(node)
    finished = harness.store.get_run(run.id)
    assert (finished.status, finished.error) == ("failed", error)
    assert harness.store.list_proposals() == []
    assert len(harness.store.list_runs()) == 1  # no critic after a failure
    assert harness.events[-1]["type"] == "run.finished"
    assert harness.events[-1]["run"]["status"] == "failed"


async def test_a_missing_claude_is_reported(
    harness: Harness, node: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APM_CLAUDE", "/nonexistent/claude")
    run = harness.runner.start("apm-assistant", node, "What is this?")
    await harness.runner.wait(node)
    finished = harness.store.get_run(run.id)
    assert finished.status == "failed"
    assert finished.error is not None
    assert finished.error.startswith("couldn't start claude")


async def test_cancel_stops_the_process(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("hang")
    run = harness.runner.start("apm-decomposer", node)
    while not harness.store.list_events(run.id):  # wait until it is really running
        await asyncio.sleep(0.02)
    await asyncio.wait_for(harness.runner.cancel(run.id), 10)

    assert harness.store.get_run(run.id).status == "cancelled"
    assert harness.runner.active_nodes() == []
    assert harness.store.list_proposals() == []
    assert harness.events[-1]["run"]["status"] == "cancelled"
    await harness.runner.cancel(run.id)  # cancelling a finished run does nothing


async def test_limits_one_run_per_node_and_max_concurrent(
    harness: Harness, project: Project, fake_claude: FakeClaude
) -> None:
    a, b, c = create_nodes(
        project, [NewNode(title="A"), NewNode(title="B"), NewNode(title="C")]
    ).created
    fake_claude.mode("hang")
    harness.runner.start("apm-assistant", a)
    with pytest.raises(RunnerBusy, match="already active on this node"):
        harness.runner.start("apm-decomposer", a)
    harness.runner.start("apm-assistant", b)
    with pytest.raises(RunnerBusy, match=r"2 runs are already active \(runner.max_concurrent\)"):
        harness.runner.start("apm-assistant", c)
    assert harness.runner.active_nodes() == sorted([a, b])
    await harness.runner.shutdown()
    assert harness.runner.active_nodes() == []
    assert {r.status for r in harness.store.list_runs()} == {"cancelled"}


async def test_continue_resumes_the_session(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    first = harness.runner.start("apm-assistant", node, "What is this?")
    await harness.runner.wait(node)
    first = harness.store.get_run(first.id)

    follow_up = harness.runner.continue_run(first.id, "And what next?")
    await harness.runner.wait(node)
    follow_up = harness.store.get_run(follow_up.id)
    assert (follow_up.status, follow_up.parent_run, follow_up.session_id) == (
        "succeeded",
        first.id,
        first.session_id,
    )
    call = fake_claude.calls[-1]
    assert option(call, "--resume") == first.session_id
    assert option(call, "-p") == "And what next?"
    assert call["cwd"] == str(harness.project.code_dir)
    text = harness.store.list_events(follow_up.id)[1].data["message"]["content"][0]["text"]
    assert text == "I am apm-assistant (resumed)"

    with pytest.raises(RunRefused, match="the message is empty"):
        harness.runner.continue_run(first.id, "  ")


async def test_the_implementer_gets_a_worktree_node_scope_and_extra_tools(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    config = harness.project.apm_dir / "apm.yaml"
    config.write_text(
        config.read_text()
        + "runner:\n  agents:\n    apm-implementer:\n      model: opus\n      budget_usd: 5\n"
        '      allowed_tools: ["Bash(uv run *)"]\n'
    )
    run = harness.runner.start("apm-implementer", node)
    await harness.runner.wait(node)
    assert harness.store.get_run(run.id).status == "succeeded"
    assert harness.store.list_proposals() == []

    (call,) = fake_claude.calls
    assert option(call, "--worktree") == f"apm-{handle(node)}-file-i-o"
    assert option(call, "--permission-mode") == "acceptEdits"
    assert option(call, "--model") == "opus"
    assert option(call, "--max-budget-usd") == "5"
    assert "--json-schema" not in call["argv"]
    assert f"Its full id is {node}" in option(call, "-p")
    allowed = option(call, "--allowedTools").split(",")
    assert {"Edit", "Write", "Bash(git *)", "Bash(uv run *)"} <= set(allowed)
    assert "mcp__apm__apm_set_status" in allowed
    assert "mcp__apm__apm_create_nodes" not in allowed
    mcp = json.loads(Path(option(call, "--mcp-config")).read_text())["mcpServers"]["apm"]
    assert mcp["env"]["APM_RUN_SCOPE"] == f"node:{node}"


async def test_the_implementer_is_only_offered_for_work_that_can_start(
    harness: Harness, project: Project
) -> None:
    parent = create_nodes(project, [NewNode(title="Parent")]).created[0]
    first, second = create_nodes(
        project,
        [
            NewNode(title="First", parent=parent, key="a"),
            NewNode(title="Second", parent=parent, depends_on_keys=["a"]),
        ],
    ).created
    with pytest.raises(RunRefused, match="only a leaf can be implemented"):
        harness.runner.start("apm-implementer", parent)
    with pytest.raises(RunRefused, match=f"not ready: waiting on {handle(first)}"):
        harness.runner.start("apm-implementer", second)
    set_status(project, first, Status.DONE)
    with pytest.raises(RunRefused, match="this node is done"):
        harness.runner.start("apm-implementer", first)
    with pytest.raises(RunRefused, match="unknown agent"):
        harness.runner.start("apm-critic", first)
    with pytest.raises(RunRefused, match="unknown agent"):
        harness.runner.start("nope", first)
    assert harness.store.list_runs() == []


async def test_runs_cut_off_by_a_dead_host_are_marked_failed(project: Project) -> None:
    store = Store(project.apm_dir)
    node = create_nodes(project, [NewNode(title="N")]).created[0]
    orphan = store.create_run(node_id=node, agent="apm-assistant", pid=2**22 + 12345)
    Runner(project, store)
    finished = store.get_run(orphan.id)
    assert (finished.status, finished.error) == ("failed", "interrupted: its host process exited")
