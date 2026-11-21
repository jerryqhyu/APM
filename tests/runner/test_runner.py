import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from apm.core.files import read_body
from apm.core.ids import handle
from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, set_status
from apm.core.project import Project, load_graph
from apm.core.store import Store
from apm.runner.agents import RunRefused
from apm.runner.runner import Runner, RunnerBusy, _failure
from tests.conftest import CodeRepo
from tests.fake_gh import NEW_PR, FakeGh
from tests.runner.fake import FakeClaude


class Harness:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.store = Store(project.apm_dir)
        self.events: list[dict[str, Any]] = []
        self.runner = Runner(project, self.store, self.events.append)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    async def next_request(self) -> dict[str, Any]:
        """Waits until a run asks the user something."""
        for _ in range(500):
            if requests := self.runner.pending_requests():
                return requests[0]
            await asyncio.sleep(0.01)
        raise AssertionError("no request arrived")

    def said(self, run_id: str) -> list[str]:
        """The text the agent said during a run."""
        out = []
        for event in self.store.list_events(run_id):
            if event.data["type"] == "assistant":
                out += [block["text"] for block in event.data["message"]["content"]]
        return out


@pytest.fixture
def harness(project: Project, fake_claude: FakeClaude) -> Harness:
    return Harness(project)


@pytest.fixture
def node(project: Project) -> str:
    return create_nodes(project, [NewNode(title="File I/O")]).created[0]


def option(call: dict, name: str) -> str:
    argv = call["argv"]
    return argv[argv.index(name) + 1]


def mcp_env(call: dict) -> dict[str, str]:
    return json.loads(Path(option(call, "--mcp-config")).read_text())["mcpServers"]["apm"]["env"]


async def test_a_decomposer_run_becomes_a_reviewed_proposal(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    run = harness.runner.start("apm-decomposer", node, "Keep it small.")
    assert (run.status, run.mode) == ("running", "auto")
    await harness.runner.wait(run.id)

    decomposer, critic = sorted(harness.store.list_runs(), key=lambda r: r.started)
    assert (decomposer.id, decomposer.agent, decomposer.status) == (
        run.id,
        "apm-decomposer",
        "succeeded",
    )
    assert (critic.agent, critic.status, critic.parent_run) == ("apm-critic", "succeeded", run.id)
    assert critic.mode == "dontAsk"  # the reviewer never stops to ask
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
    assert harness.runner.active() == 0

    # Only the events a transcript needs are stored: noise and non-JSON lines are dropped.
    stored = [e.data["type"] for e in harness.store.list_events(run.id)]
    assert stored == ["system", "assistant", "result"]

    first, second = fake_claude.calls
    assert first["cwd"] == str(harness.project.code_dir)
    assert first["prompt"].startswith(f'Decompose node {handle(node)} ("File I/O")')
    assert "Keep it small." in first["prompt"]
    assert option(first, "--agent") == "apm:apm-decomposer"
    assert option(first, "--session-id") == decomposer.session_id
    assert option(first, "--input-format") == "stream-json"
    assert option(first, "--permission-prompt-tool") == "stdio"
    assert option(first, "--permission-mode") == "auto"
    assert option(first, "--max-budget-usd") == "2"
    assert option(first, "--disallowedTools") == "Write,Edit,NotebookEdit"
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
    assert option(second, "--permission-mode") == "dontAsk"
    assert '"title": "Parse"' in second["prompt"]
    assert mcp_env(first) == {
        "APM_PROJECT": str(harness.project.code_dir),
        "APM_RUN_SCOPE": "read",
        "APM_ACTOR": "agent:apm-decomposer",
        "APM_RUN": run.id,
        "APM_NODE": node,
    }


async def test_a_revise_verdict_gets_one_revision(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("revise")
    run = harness.runner.start("apm-decomposer", node)
    await harness.runner.wait(run.id)

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
    assert "Too big." in third["prompt"]


async def test_the_critic_can_be_skipped_or_fail_without_losing_the_proposal(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    run = harness.runner.start("apm-decomposer", node, critic=False)
    await harness.runner.wait(run.id)
    assert [r.agent for r in harness.store.list_runs()] == ["apm-decomposer"]
    assert harness.store.list_proposals()[0].review is None

    fake_claude.mode("critic-fails")
    run = harness.runner.start("apm-decomposer", node)
    await harness.runner.wait(run.id)
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
    await harness.runner.wait(run.id)
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
    await harness.runner.wait(run.id)
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
    assert harness.runner.active() == 0
    assert harness.store.list_proposals() == []
    assert harness.events[-1]["run"]["status"] == "cancelled"
    await harness.runner.cancel(run.id)  # cancelling a finished run does nothing


async def test_limits_one_at_a_time_per_node_and_max_concurrent(
    harness: Harness, project: Project, fake_claude: FakeClaude
) -> None:
    a, b, c = create_nodes(
        project, [NewNode(title="A"), NewNode(title="B"), NewNode(title="C")]
    ).created
    fake_claude.mode("hang")
    harness.runner.start("apm-decomposer", a)
    with pytest.raises(RunnerBusy, match="Decompose is already running on this node"):
        harness.runner.start("apm-decomposer", a)
    # A conversation is its own slot: chatting about a node being decomposed is fine.
    chat = harness.runner.start("apm-assistant", a, "Why?")
    with pytest.raises(RunnerBusy, match="Chat is already running in this conversation"):
        harness.runner.continue_run(chat.id, "And?")
    with pytest.raises(RunnerBusy, match=r"2 runs are already active \(runner.max_concurrent\)"):
        harness.runner.start("apm-assistant", c)
    assert harness.runner.active() == 2
    await harness.runner.shutdown()
    assert harness.runner.active() == 0
    assert {r.status for r in harness.store.list_runs()} == {"cancelled"}
    assert b not in {r.node_id for r in harness.store.list_runs()}


async def test_a_conversation_continues_in_the_same_session(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    first = harness.runner.start("apm-assistant", node, "What is this?", mode="manual")
    await harness.runner.wait(first.id)
    first = harness.store.get_run(first.id)

    follow_up = harness.runner.continue_run(first.id, "And what next?")
    await harness.runner.wait(follow_up.id)
    follow_up = harness.store.get_run(follow_up.id)
    assert (follow_up.status, follow_up.parent_run, follow_up.session_id) == (
        "succeeded",
        first.id,
        first.session_id,
    )
    assert follow_up.mode == "manual"  # a conversation keeps its mode unless you change it
    call = fake_claude.calls[-1]
    assert option(call, "--resume") == first.session_id
    assert option(call, "--permission-mode") == "manual"
    assert call["prompt"] == "And what next?"
    assert call["cwd"] == str(harness.project.code_dir)
    assert harness.said(follow_up.id) == ["I am apm-assistant (resumed)"]

    switched = harness.runner.continue_run(follow_up.id, "Go on.", mode="plan")
    await harness.runner.wait(switched.id)
    assert option(fake_claude.calls[-1], "--permission-mode") == "plan"

    with pytest.raises(RunRefused, match="the message is empty"):
        harness.runner.continue_run(first.id, "  ")


async def test_the_chat_can_be_about_the_whole_project_and_may_change_the_plan(
    harness: Harness, fake_claude: FakeClaude
) -> None:
    run = harness.runner.start("apm-assistant", None, "Plan a todo app with me.")
    await harness.runner.wait(run.id)
    finished = harness.store.get_run(run.id)
    assert (finished.status, finished.node_id) == ("succeeded", "")

    (call,) = fake_claude.calls
    assert call["prompt"] == "Plan a todo app with me."
    env = mcp_env(call)
    assert (env["APM_RUN_SCOPE"], env["APM_NODE"]) == ("project", "")
    allowed = option(call, "--allowedTools").split(",")
    assert {"mcp__apm__apm_create_nodes", "mcp__apm__apm_merge_nodes"} <= set(allowed)
    assert "mcp__apm__apm_delete_node" in allowed
    assert "--disallowedTools" not in call["argv"]
    assert "--json-schema" not in call["argv"]

    with pytest.raises(RunRefused, match="Decompose works on a node"):
        harness.runner.start("apm-decomposer", None)


async def test_a_permission_prompt_waits_for_the_users_answer(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("ask-permission", "other-control")
    run = harness.runner.start("apm-assistant", node, "Run the tests.")
    request = await harness.next_request()
    assert request == {
        "id": "p-1",
        "run_id": run.id,
        "tool": "Bash",
        "input": {"command": "make test"},
        "description": "Run the tests",
        "kind": "permission",
    }
    assert harness.store.get_run(run.id).status == "running"
    assert harness.runner.pending_requests(run.id) == [request]
    assert harness.runner.pending_requests("another-run") == []

    await harness.runner.answer("p-1", allow=True)
    await harness.runner.wait(run.id)
    assert harness.store.get_run(run.id).status == "succeeded"
    assert harness.said(run.id)[-1] == "allowed: make test"
    assert harness.runner.pending_requests() == []
    kinds = [e.data["type"] for e in harness.store.list_events(run.id)]
    assert kinds == [
        "system",
        "assistant",
        "apm_request",
        "apm_request_resolved",
        "assistant",
        "result",
    ]
    assert "run.request" in harness.types()
    assert "run.request_resolved" in harness.types()
    with pytest.raises(RunRefused, match="no longer waiting"):
        await harness.runner.answer("p-1", allow=True)


async def test_a_denied_permission_is_passed_on_with_the_reason(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("ask-permission")
    run = harness.runner.start("apm-assistant", node, "Run the tests.")
    await harness.next_request()
    await harness.runner.answer("p-1", allow=False, message="Not on a Friday.")
    await harness.runner.wait(run.id)
    assert harness.said(run.id)[-1] == "denied: Not on a Friday."
    resolved = harness.store.list_events(run.id)[3].data
    assert (resolved["allow"], resolved["message"]) == (False, "Not on a Friday.")


async def test_a_question_is_answered_with_the_chosen_option(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("ask-question")
    run = harness.runner.start("apm-assistant", node, "Set up formatting.")
    request = await harness.next_request()
    assert (request["kind"], request["tool"]) == ("question", "AskUserQuestion")
    assert request["input"]["questions"][0]["options"][1]["label"] == "Spaces"

    await harness.runner.answer("q-1", allow=True, answers={"Tabs or spaces?": "Spaces"})
    await harness.runner.wait(run.id)
    assert harness.said(run.id)[-1] == "you chose: Spaces"


async def test_stopping_a_run_drops_what_it_was_waiting_for(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    fake_claude.mode("ask-permission")
    run = harness.runner.start("apm-assistant", node, "Run the tests.")
    await harness.next_request()
    await harness.runner.cancel(run.id)
    assert harness.store.get_run(run.id).status == "cancelled"
    assert harness.runner.pending_requests() == []


async def test_the_implementer_gets_a_worktree_node_scope_and_extra_tools(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    config = harness.project.apm_dir / "apm.yaml"
    config.write_text(
        config.read_text()
        + "runner:\n  permission_mode: acceptEdits\n  agents:\n    apm-implementer:\n"
        '      model: opus\n      budget_usd: 5\n      allowed_tools: ["Bash(uv run *)"]\n'
    )
    run = harness.runner.start("apm-implementer", node)
    await harness.runner.wait(run.id)
    assert harness.store.get_run(run.id).status == "succeeded"
    assert harness.store.get_run(run.id).mode == "acceptEdits"  # the project's default
    assert harness.store.list_proposals() == []

    (call,) = fake_claude.calls
    assert option(call, "--worktree") == f"apm-{handle(node)}-file-i-o"
    assert option(call, "--permission-mode") == "acceptEdits"
    assert option(call, "--model") == "opus"
    assert option(call, "--max-budget-usd") == "5"
    assert "--json-schema" not in call["argv"]
    assert "--disallowedTools" not in call["argv"]
    assert f"Its full id is {node}" in call["prompt"]
    allowed = option(call, "--allowedTools").split(",")
    assert {"Edit", "Write", "Bash(git *)", "Bash(uv run *)"} <= set(allowed)
    assert "mcp__apm__apm_set_status" in allowed
    assert "mcp__apm__apm_create_nodes" not in allowed
    assert mcp_env(call)["APM_RUN_SCOPE"] == f"node:{node}"


async def test_work_stacked_on_a_delivered_dependency_can_be_implemented(
    harness: Harness, project: Project
) -> None:
    first, second = create_nodes(
        project,
        [NewNode(title="First", key="a"), NewNode(title="Second", depends_on_keys=["a"])],
    ).created
    set_status(project, first, Status.IN_PROGRESS)
    attach_delivery(project, first, branch="apm/first", pr="https://github.com/o/r/pull/20")
    run = harness.runner.start("apm-implementer", second)
    await harness.runner.wait(run.id)
    assert harness.store.get_run(run.id).status == "succeeded"


async def test_runs_are_refused_when_they_cannot_start(harness: Harness, project: Project) -> None:
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
    set_status(project, first, Status.IN_PROGRESS)
    with pytest.raises(RunRefused, match=f"not ready: waiting on {handle(first)}"):
        harness.runner.start("apm-implementer", second)  # in progress, nothing pushed yet
    set_status(project, first, Status.DONE)
    with pytest.raises(RunRefused, match="this node is done"):
        harness.runner.start("apm-implementer", first)
    with pytest.raises(RunRefused, match="unknown agent"):
        harness.runner.start("apm-critic", first)
    with pytest.raises(RunRefused, match="unknown agent"):
        harness.runner.start("nope", first)
    with pytest.raises(RunRefused, match="unknown permission mode 'yolo'"):
        harness.runner.start("apm-assistant", first, mode="yolo")
    assert harness.store.list_runs() == []


async def test_runs_cut_off_by_a_dead_host_are_marked_failed(project: Project) -> None:
    store = Store(project.apm_dir)
    node = create_nodes(project, [NewNode(title="N")]).created[0]
    orphan = store.create_run(node_id=node, agent="apm-assistant", pid=2**22 + 12345)
    Runner(project, store)
    finished = store.get_run(orphan.id)
    assert (finished.status, finished.error) == ("failed", "interrupted: its host process exited")


def test_failure_messages_say_what_went_wrong() -> None:
    limit = {"is_error": True, "subtype": "success", "result": "You've hit your 5-hour limit"}
    assert _failure(limit, "", 0) == "You've hit your 5-hour limit"
    budget = {"is_error": True, "subtype": "error_max_budget_usd", "result": "over budget"}
    assert _failure(budget, "", 0) == "error_max_budget_usd: over budget"
    assert _failure({"is_error": True, "errors": ["a", "b"]}, "", 1) == "error: a, b"
    assert _failure({}, "warning\nboom\n", 2) == "claude exited with code 2: boom"
    assert _failure({}, "", 3) == "claude exited with code 3"


async def test_a_conversation_whose_first_turn_never_started_can_continue(
    harness: Harness, node: str, fake_claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.environ["APM_CLAUDE"]
    monkeypatch.setenv("APM_CLAUDE", "/nonexistent/claude")
    first = harness.runner.start("apm-assistant", node, "Hello?")
    await harness.runner.wait(first.id)
    assert harness.store.get_run(first.id).status == "failed"

    monkeypatch.setenv("APM_CLAUDE", real)
    second = harness.runner.continue_run(first.id, "Are you there?")
    await harness.runner.wait(second.id)
    assert harness.store.get_run(second.id).status == "succeeded"
    (call,) = fake_claude.calls
    # No session existed, so this turn starts a new one instead of resuming it...
    assert option(call, "--session-id") != harness.store.get_run(first.id).session_id
    assert "--resume" not in call["argv"]
    assert harness.store.get_run(second.id).parent_run == first.id  # still one conversation
    # ...and it carries the context a first message would have.
    assert call["prompt"].startswith(f"The user is looking at node {handle(node)}")
    assert call["prompt"].endswith("Are you there?")

    # Once it has started, the next turn resumes as usual.
    third = harness.runner.continue_run(second.id, "Thanks.")
    await harness.runner.wait(third.id)
    assert "--resume" in fake_claude.calls[-1]["argv"]


async def test_the_reviewer_cannot_be_replied_to(
    harness: Harness, node: str, fake_claude: FakeClaude
) -> None:
    run = harness.runner.start("apm-decomposer", node)
    await harness.runner.wait(run.id)
    critic = next(r for r in harness.store.list_runs() if r.agent == "apm-critic")
    with pytest.raises(RunRefused, match="Review runs on its own"):
        harness.runner.continue_run(critic.id, "Why?")


# --- an implementer's base branch and pull request ---


def notices(harness: Harness, run_id: str) -> list[tuple[str, str]]:
    return [
        (e.data["level"], e.data["text"])
        for e in harness.store.list_events(run_id)
        if e.data["type"] == "apm_notice"
    ]


def stacked_pair(code_repo: CodeRepo) -> tuple[str, str]:
    """`first`, done and delivered on an unmerged branch, and `second`, which depends on it."""
    project = code_repo.project
    first, second = create_nodes(
        project,
        [NewNode(title="First", key="a"), NewNode(title="Second", depends_on_keys=["a"])],
    ).created
    code_repo.push_branch("apm/first")
    attach_delivery(project, first, branch="apm/first", pr="https://github.com/o/r/pull/1")
    set_status(project, first, Status.DONE)
    return first, second


async def test_the_implementer_is_told_to_stack_on_its_dependency(
    harness: Harness, code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    first, second = stacked_pair(code_repo)
    run = harness.runner.start("apm-implementer", second, "Keep it small.")
    await harness.runner.wait(run.id)

    (call,) = fake_claude.calls
    prompt = call["prompt"]
    assert f'It builds on {handle(first)} ("First"), whose branch apm/first is not merged' in prompt
    assert "start from origin/apm/first" in prompt
    assert "`gh pr create --base apm/first`" in prompt
    assert prompt.endswith("Guidance from the user:\nKeep it small.")
    assert harness.store.get_run(run.id).prompt == prompt  # what the conversation shows


async def test_after_the_run_apm_opens_the_missing_pull_request(
    harness: Harness, code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    _, second = stacked_pair(code_repo)
    # As if the agent pushed its branch and recorded it, but couldn't open the PR.
    code_repo.push_branch("apm/second", base="apm/first")
    attach_delivery(code_repo.project, second, branch="apm/second")
    run = harness.runner.start("apm-implementer", second)
    await harness.runner.wait(run.id)

    assert harness.store.get_run(run.id).status == "succeeded"
    node = load_graph(code_repo.project)[second]
    assert node.delivery is not None
    assert node.delivery.pr == NEW_PR
    assert node.status is Status.TODO  # opening a PR marks nothing done
    assert fake_gh.field(fake_gh.calls[-1], "base") == "apm/first"
    assert notices(harness, run.id) == [
        ("info", f"APM opened the pull request against apm/first: {NEW_PR}")
    ]
    assert "apm_notice" in [e["event"]["data"]["type"] for e in harness.events if "event" in e]


async def test_when_no_pull_request_can_be_opened_the_run_says_why(
    harness: Harness, code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    (node,) = create_nodes(code_repo.project, [NewNode(title="Solo")]).created
    attach_delivery(code_repo.project, node, branch="apm/solo")
    fake_gh.fail()
    run = harness.runner.start("apm-implementer", node)
    await harness.runner.wait(run.id)

    ((level, text),) = notices(harness, run.id)
    assert level == "warning"
    assert text.startswith("No pull request yet: `gh api` failed: No commits between.")
    assert "Branch `apm/solo` is pushed" in text
    body = read_body(code_repo.project.apm_dir, node)
    assert "**No pull request yet.** Branch `apm/solo` is delivered" in body
    graph = load_graph(code_repo.project)
    assert graph[node].status is Status.TODO
    delivery = graph[node].delivery
    assert delivery is not None
    assert delivery.pr is None


async def test_a_run_that_delivered_nothing_says_so_in_the_chat_only(
    harness: Harness, node: str, fake_gh: FakeGh
) -> None:
    before = read_body(harness.project.apm_dir, node)
    run = harness.runner.start("apm-implementer", node)
    await harness.runner.wait(run.id)
    assert notices(harness, run.id) == [
        (
            "warning",
            "No pull request yet: the run recorded no branch, so there is nothing to open one"
            " from.",
        )
    ]
    assert read_body(harness.project.apm_dir, node) == before
    assert fake_gh.calls == []
