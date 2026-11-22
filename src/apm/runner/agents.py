"""The agents APM can start, and the exact `claude` command line for each (plans.md §7.1)."""

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apm.core.config import ApmConfig, FeaturesConfig
from apm.core.delivery import Base
from apm.core.errors import ApmError
from apm.core.feature_review import FEATURE_REVIEW_SCHEMA, Target
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import NodeId, Status
from apm.core.project import Project
from apm.core.proposals import DECOMPOSITION_SCHEMA, REVIEW_SCHEMA
from apm.core.settings import feature_settings
from apm.core.store import PROJECT
from apm.core.task_verdict import TASK_VERDICT_SCHEMA

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"
CLAUDE_ENV = "APM_CLAUDE"
"""Overrides the `claude` executable: for a wrapper, or for tests."""

READ_MCP = ("apm_get_graph", "apm_get_node", "apm_search", "apm_ready", "apm_check")
WRITE_MCP = (
    "apm_create_nodes",
    "apm_update_node",
    "apm_move_node",
    "apm_link",
    "apm_unlink",
    "apm_set_status",
    "apm_attach_delivery",
    "apm_delete_node",
    "apm_merge_nodes",
)
READ_TOOLS = ("Read", "Grep", "Glob")
GIT_READ_TOOLS = ("Bash(git log *)", "Bash(git diff *)", "Bash(git show *)", "Bash(git grep *)")
EDIT_TOOLS = ("Write", "Edit", "NotebookEdit")

MODES: dict[str, str] = {
    "auto": "Auto: Claude decides what is safe and asks you about the rest",
    "manual": "Ask: you approve every change and command",
    "acceptEdits": "Accept edits: file edits go ahead, commands ask",
    "plan": "Plan first: Claude proposes a plan and waits for your go-ahead",
    "dontAsk": "Don't ask: anything not already allowed is refused",
}
"""The permission modes a run can be started in: Claude Code's own, by its names."""
DEFAULT_MODE = "auto"


@dataclass(frozen=True, slots=True)
class AgentSpec:
    name: str
    label: str
    summary: str
    """What it does, for the UI."""
    scope: Literal["read", "node", "project"]
    """What it may write to the plan through the MCP: nothing, its own node, or anything."""
    tools: tuple[str, ...]
    """Built-in tools it may use without asking. Anything else follows the permission mode."""
    mcp_tools: tuple[str, ...]
    schema: dict[str, Any] | None = None
    """JSON schema for its final answer, if it must be structured."""
    worktree: bool = False
    """Work in its own git worktree, so your checkout is never touched."""
    needs_node: bool = True
    """False if it can also be started on the project as a whole."""
    read_only: bool = False
    """Never edits files, whatever the permission mode."""
    user_startable: bool = True


AGENTS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (
        AgentSpec(
            name="apm-assistant",
            label="Chat",
            summary="Talk the plan through with Claude. It can read your code and change the"
            " plan as you go: add, edit, link, move, merge and delete nodes.",
            scope="project",
            tools=READ_TOOLS,
            mcp_tools=(*READ_MCP, *WRITE_MCP),
            needs_node=False,
        ),
        AgentSpec(
            name="apm-decomposer",
            label="Decompose",
            summary="Break this node into children. You review the proposal before anything is"
            " written.",
            scope="read",
            tools=READ_TOOLS,
            mcp_tools=READ_MCP,
            schema=DECOMPOSITION_SCHEMA,
            read_only=True,
        ),
        AgentSpec(
            name="apm-critic",
            label="Review",
            summary="Review a proposed breakdown.",
            scope="read",
            tools=READ_TOOLS,
            mcp_tools=READ_MCP,
            schema=REVIEW_SCHEMA,
            read_only=True,
            user_startable=False,
        ),
        AgentSpec(
            name="apm-implementer",
            label="Implement",
            summary="Build this node in its own worktree and open a pull request.",
            scope="node",
            tools=(*READ_TOOLS, "Edit", "Write", "Bash(git *)", "Bash(gh pr *)"),
            mcp_tools=(*READ_MCP, "apm_set_status", "apm_attach_delivery", "apm_update_node"),
            worktree=True,
        ),
        AgentSpec(
            name="apm-feature-reviewer",
            label="Review feature",
            summary="Check the feature branch against this node's description, once its tasks are"
            " merged: is every acceptance criterion really met? Gaps become follow-up tasks.",
            scope="read",
            tools=(*READ_TOOLS, *GIT_READ_TOOLS),
            mcp_tools=READ_MCP,
            schema=FEATURE_REVIEW_SCHEMA,
            read_only=True,
        ),
        AgentSpec(
            name="apm-task-reviewer",
            label="Review task",
            summary="Check that this task is small enough to read in five minutes: at most the"
            " project's task size limit (50 lines of real code by default). Too big, and it"
            " proposes a split; too vague, and it says what is missing.",
            scope="read",
            tools=READ_TOOLS,
            mcp_tools=READ_MCP,
            schema=TASK_VERDICT_SCHEMA,
            read_only=True,
        ),
    )
}


class RunRefused(ApmError, ValueError):  # noqa: N818 (a refusal, not a failure)
    """The run can't start, for a reason the user can fix."""


def check_startable(
    spec: AgentSpec,
    graph: Graph,
    node_id: NodeId,
    implement_gate: Callable[[NodeId], str | None] | None = None,
) -> None:
    """Raises `RunRefused` if `spec` shouldn't run on this node now. `implement_gate` is
    `implement_blocker` with the project's store, config and bodies bound.
    """
    if node_id == PROJECT:
        if spec.needs_node:
            raise RunRefused(f"{spec.label} works on a node: pick one first")
        return
    node = graph[node_id]
    reviewless = ("apm-implementer", "apm-decomposer", "apm-task-reviewer")
    if graph.is_review(node_id) and spec.name in reviewless:
        raise RunRefused(
            "this is a feature's review step: run Review feature on it once its tasks are done"
        )
    if spec.name == "apm-task-reviewer" and not graph.is_leaf(node_id):
        raise RunRefused("only a task can be reviewed: this node has children")
    if spec.name != "apm-implementer":
        return
    if not graph.is_leaf(node_id):
        raise RunRefused("only a leaf can be implemented: this node has children")
    state = graph.states[node_id]
    if node.status is Status.TODO and not state.ready:
        blocking = [n for n in state.waiting_on if n not in state.stacked_on]
        waiting = ", ".join(handle(n) for n in blocking) or "a dropped ancestor"
        raise RunRefused(f"not ready: waiting on {waiting}")
    if node.status in (Status.DONE, Status.DROPPED):
        raise RunRefused(f"this node is {node.status}")
    if implement_gate is not None and (reason := implement_gate(node_id)):
        raise RunRefused(reason)


def check_mode(mode: str | None, config: ApmConfig) -> str:
    """The mode to run in: the one asked for, else the project's default."""
    chosen = mode or config.runner.permission_mode
    if chosen not in MODES:
        raise RunRefused(f"unknown permission mode {chosen!r}: pick one of {', '.join(MODES)}")
    return chosen


def slug(title: str, limit: int = 32) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return words[:limit].rstrip("-") or "node"


def worktree_name(graph: Graph, node_id: NodeId) -> str:
    return f"apm-{handle(node_id)}-{slug(graph[node_id].title)}"


def build_prompt(
    spec: AgentSpec,
    graph: Graph,
    node_id: NodeId,
    message: str,
    *,
    config: ApmConfig | None = None,
    base: Base | None = None,
) -> str:
    """The first message of a run: what the user typed, with where they are looking. An
    implementer's also says which branch to start from and open its pull request against. The
    decomposer, the implementer and the task reviewer are told the task size limit that applies
    (`feature_settings`; the default without `config`).
    """
    message = message.strip()
    if node_id == PROJECT:
        return message or "Give me a short overview of the plan: where it stands and what's next."
    node = graph[node_id]
    where = f'node {handle(node_id)} ("{node.title}") of the APM plan'
    limit = task_limit(config, graph, node_id)
    if spec.name == "apm-decomposer":
        prompt = (
            f"Decompose {where} into child nodes and return the proposal. Size the tasks you"
            f" propose to the task size limit here, {limit} real lines of code each: a task review"
            " checks every task against it before it is built."
        )
        return prompt + (f"\n\nGuidance from the user:\n{message}" if message else "")
    if spec.name == "apm-implementer":
        prompt = (
            f"Implement {where}. Its full id is {node_id}; use it in the APM-Node trailer.\n"
            f"Work on branch apm/{handle(node_id)}-{slug(node.title)}.\n"
            f"{base_instructions(graph, base)}\n"
            f"Aim to keep the change within {limit} real lines of code, the task size limit its"
            " task review applied."
        )
        return prompt + (f"\n\nGuidance from the user:\n{message}" if message else "")
    if spec.name == "apm-feature-reviewer":
        prompt = f"Review the feature in {where}: is it fully built?"
        return prompt + (f"\n\nGuidance from the user:\n{message}" if message else "")
    if spec.name == "apm-task-reviewer":
        prompt = (
            f"Review the task in {where}: size it with the apm-task-sizing skill. The limit for"
            f" this task is {limit} real lines of code."
        )
        return prompt + (f"\n\nGuidance from the user:\n{message}" if message else "")
    if spec.name == "apm-assistant":
        question = message or "Summarise this node: what it is for, its state, and what's next."
        return f"The user is looking at {where}.\n\n{question}"
    raise RunRefused(f"{spec.name} can't be started directly")


def task_limit(config: ApmConfig | None, graph: Graph, node_id: NodeId) -> int:
    """The task size limit for a node, in real lines of code: APM's default without `config`."""
    if config is None:
        return FeaturesConfig().task_max_lines
    return feature_settings(config, graph, node_id).task_max_lines


def base_instructions(graph: Graph, base: Base | None) -> str:
    """Where an implementer starts, and what its pull request targets (see `choose_base`)."""
    if base is None or base.branch is None:
        return (
            "Start it from the repository's default branch, and open the pull request against it."
        )
    branch, into = base.branch, base.pr_base
    target = f"open the pull request against {into} (`gh pr create --base {into}`)"
    if base.stacked_on is not None:
        dep = base.stacked_on
        title = graph[dep].title if dep in graph else "a dependency"
        return (
            f'It builds on {handle(dep)} ("{title}"), whose branch {branch} is not merged yet, so'
            f" stack on it: start from origin/{branch} and {target}."
            + (
                " The pull request then shows only this node's changes."
                if into == branch
                else f" Until {branch} is merged, the pull request also shows its changes."
            )
        )
    if base.feature is not None and base.feature in graph:
        feature = graph[base.feature]
        text = (
            f'It is a task of feature {handle(base.feature)} ("{feature.title}"), whose branch is'
            f" {branch}: start from origin/{branch}, and {target}. The feature goes on to the"
            " default branch later, through its review step, once all its tasks are merged."
        )
    else:
        text = f"Start it from origin/{branch}, and {target}."
    if base.unmerged:
        waits = ", ".join(f"{handle(node)} ({name})" for node, name in base.unmerged)
        text += (
            f" Work it depends on is not merged yet: {waits}. A branch can stack on only one of"
            f" them, so build on {branch} and say in the pull request that those must be merged"
            " first."
        )
    return text


def feature_review_prompt(graph: Graph, node_id: NodeId, target: Target, message: str = "") -> str:
    """A feature reviewer's first message: the feature, the commit it reviews, and its tasks."""
    feature = target.feature if target.feature in graph else node_id
    lines = [
        f'Review the feature in node {handle(feature)} ("{graph[feature].title}") of the APM'
        f" plan, as built on branch {target.branch} at commit {target.sha} ({target.why}).",
        "Your working directory is a checkout of that commit: read the code there.",
    ]
    if target.step is not None:
        lines.append(
            f"You are run on its review step, {handle(target.step)}: the feature's description"
            " is the spec, not the step's."
        )
    if target.default and target.default != target.branch:
        lines.append(
            f"`git diff origin/{target.default}...HEAD` shows everything the feature changes."
        )
    lines += ["", "Its tasks:"]
    for task in target.tasks:
        where = {True: "in this commit", False: "NOT in this commit", None: "can't tell"}
        delivered = task.pr or task.branch or "nothing delivered"
        lines.append(
            f'- {handle(task.node_id)} "{graph[task.node_id].title}": {task.status},'
            f" {delivered}, {where[task.included]}"
        )
    missing = [t for t in target.tasks if t.included is False]
    if missing:
        lines += [
            "",
            "Some tasks' work is not in this commit. Judge the feature by what is in it, and say"
            " what is missing.",
        ]
    lines += ["", "Return your review."]
    text = "\n".join(lines)
    return text + (f"\n\nGuidance from the user:\n{message.strip()}" if message.strip() else "")


def critic_prompt(graph: Graph, node_id: NodeId, proposal: dict[str, Any]) -> str:
    node = graph[node_id]
    return (
        f'Review this proposed breakdown of node {handle(node_id)} ("{node.title}") of the APM'
        " plan, and return your review.\n\n"
        f"```json\n{json.dumps(proposal, indent=2, ensure_ascii=False)}\n```"
    )


def revision_prompt(review: dict[str, Any]) -> str:
    return (
        "A reviewer looked at your breakdown and asks for a revision. Fix the major issues, take"
        " the minor ones where you agree, and return the whole breakdown again.\n\n"
        f"```json\n{json.dumps(review, indent=2, ensure_ascii=False)}\n```"
    )


def mcp_config(project: Project, spec: AgentSpec, node_id: NodeId, run_id: str) -> dict[str, Any]:
    """The per-run MCP config: the same `apm mcp` server, with this run's project and scope."""
    scope = f"node:{node_id}" if spec.scope == "node" else spec.scope
    return {
        "mcpServers": {
            "apm": {
                "command": sys.executable,
                "args": ["-m", "apm", "mcp"],
                "env": {
                    "APM_PROJECT": str(project.code_dir),
                    "APM_RUN_SCOPE": scope,
                    "APM_ACTOR": f"agent:{spec.name}",
                    "APM_RUN": run_id,
                    "APM_NODE": node_id,
                },
            }
        }
    }


def build_command(
    spec: AgentSpec,
    config: ApmConfig,
    *,
    mcp_config_path: Path,
    session_id: str,
    mode: str,
    resume: bool = False,
    worktree: str | None = None,
) -> list[str]:
    """The prompt is not here: it is sent on stdin, which stays open so that permission prompts
    and questions can be answered (plans.md §7.1 says why each flag is there).
    """
    settings = config.runner.agents.get(spec.name)
    budget = (settings.budget_usd if settings else None) or config.runner.default_budget_usd
    extra_tools = (settings.allowed_tools if settings else None) or []
    allowed = [*spec.tools, *extra_tools, *(f"mcp__apm__{tool}" for tool in spec.mcp_tools)]
    command = [
        os.environ.get(CLAUDE_ENV) or "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-prompt-tool",
        "stdio",
        "--permission-mode",
        mode,
        "--agent",
        f"apm:{spec.name}",
        "--plugin-dir",
        str(PLUGIN_DIR),
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--allowedTools",
        ",".join(dict.fromkeys(allowed)),
        "--max-budget-usd",
        f"{budget:g}",
    ]
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    if spec.read_only:
        command += ["--disallowedTools", ",".join(EDIT_TOOLS)]
    if settings and settings.model:
        command += ["--model", settings.model]
    if spec.schema is not None:
        command += ["--json-schema", json.dumps(spec.schema, separators=(",", ":"))]
    if worktree and not resume:
        command += ["--worktree", worktree]
    return command
