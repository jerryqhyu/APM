"""The agents APM can start, and the exact `claude` command line for each (plans.md §7.1)."""

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apm.core.config import ApmConfig
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import NodeId, Status
from apm.core.project import Project
from apm.core.proposals import DECOMPOSITION_SCHEMA, REVIEW_SCHEMA
from apm.core.status import compute_states

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"
CLAUDE_ENV = "APM_CLAUDE"
"""Overrides the `claude` executable: for a wrapper, or for tests."""

READ_MCP = ("apm_get_graph", "apm_get_node", "apm_search", "apm_ready", "apm_check")
READ_TOOLS = ("Read", "Grep", "Glob")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    name: str
    summary: str
    """What it does, for the UI."""
    scope: Literal["read", "node"]
    """What it may write through the MCP: nothing, or its own node."""
    tools: tuple[str, ...]
    """Built-in tools it may use without asking. Nothing else is allowed: a headless run can't
    ask, so anything not listed is denied.
    """
    mcp_tools: tuple[str, ...]
    schema: dict[str, Any] | None = None
    """JSON schema for its final answer, if it must be structured."""
    worktree: bool = False
    """Work in its own git worktree, so your checkout is never touched."""
    permission_mode: str | None = None
    user_startable: bool = True


AGENTS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (
        AgentSpec(
            name="apm-decomposer",
            summary="Break this node into children. You review the proposal before anything is"
            " written.",
            scope="read",
            tools=READ_TOOLS,
            mcp_tools=READ_MCP,
            schema=DECOMPOSITION_SCHEMA,
        ),
        AgentSpec(
            name="apm-critic",
            summary="Review a proposed breakdown.",
            scope="read",
            tools=READ_TOOLS,
            mcp_tools=READ_MCP,
            schema=REVIEW_SCHEMA,
            user_startable=False,
        ),
        AgentSpec(
            name="apm-implementer",
            summary="Build this node in its own worktree and open a pull request.",
            scope="node",
            tools=(*READ_TOOLS, "Edit", "Write", "Bash(git *)", "Bash(gh pr *)"),
            mcp_tools=(*READ_MCP, "apm_set_status", "apm_attach_delivery", "apm_update_node"),
            worktree=True,
            permission_mode="acceptEdits",
        ),
        AgentSpec(
            name="apm-assistant",
            summary="Ask a question about this node. It can read the plan and the code, and"
            " changes nothing.",
            scope="read",
            tools=READ_TOOLS,
            mcp_tools=READ_MCP,
        ),
    )
}


class RunRefused(ValueError):  # noqa: N818 (a refusal, not a failure)
    """The run can't start, for a reason the user can fix."""


def check_startable(spec: AgentSpec, graph: Graph, node_id: NodeId) -> None:
    """Raises `RunRefused` if `spec` shouldn't run on this node now."""
    node = graph[node_id]
    if spec.name != "apm-implementer":
        return
    if not graph.is_leaf(node_id):
        raise RunRefused("only a leaf can be implemented: this node has children")
    state = compute_states(graph)[node_id]
    if node.status is Status.TODO and not state.ready:
        waiting = ", ".join(handle(n) for n in state.waiting_on) or "a dropped ancestor"
        raise RunRefused(f"not ready: waiting on {waiting}")
    if node.status in (Status.DONE, Status.DROPPED):
        raise RunRefused(f"this node is {node.status}")


def slug(title: str, limit: int = 32) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return words[:limit].rstrip("-") or "node"


def worktree_name(graph: Graph, node_id: NodeId) -> str:
    return f"apm-{handle(node_id)}-{slug(graph[node_id].title)}"


def build_prompt(spec: AgentSpec, graph: Graph, node_id: NodeId, guidance: str) -> str:
    node = graph[node_id]
    where = f'node {handle(node_id)} ("{node.title}") of the APM plan'
    guidance = guidance.strip()
    if spec.name == "apm-decomposer":
        prompt = f"Decompose {where} into child nodes and return the proposal."
        return prompt + (f"\n\nGuidance from the user:\n{guidance}" if guidance else "")
    if spec.name == "apm-implementer":
        prompt = (
            f"Implement {where}. Its full id is {node_id}; use it in the APM-Node trailer.\n"
            f"Work on branch apm/{handle(node_id)}-{slug(node.title)}."
        )
        return prompt + (f"\n\nGuidance from the user:\n{guidance}" if guidance else "")
    if spec.name == "apm-assistant":
        question = guidance or "Summarise this node: what it is for, its state, and what's next."
        return f"The user is looking at {where}.\n\n{question}"
    raise RunRefused(f"{spec.name} can't be started directly")


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
    scope = f"node:{node_id}" if spec.scope == "node" else "read"
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
    prompt: str,
    mcp_config_path: Path,
    session_id: str,
    resume: bool = False,
    worktree: str | None = None,
) -> list[str]:
    """Why each flag is there is in plans.md §7.1."""
    settings = config.runner.agents.get(spec.name)
    budget = (settings.budget_usd if settings else None) or config.runner.default_budget_usd
    extra_tools = (settings.allowed_tools if settings else None) or []
    allowed = [*spec.tools, *extra_tools, *(f"mcp__apm__{tool}" for tool in spec.mcp_tools)]
    command = [
        os.environ.get(CLAUDE_ENV) or "claude",
        "-p",
        prompt,
        "--agent",
        f"apm:{spec.name}",
        "--plugin-dir",
        str(PLUGIN_DIR),
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--permission-prompts",
        "none",
        "--allowedTools",
        ",".join(dict.fromkeys(allowed)),
        "--max-budget-usd",
        f"{budget:g}",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    if settings and settings.model:
        command += ["--model", settings.model]
    if spec.schema is not None:
        command += ["--json-schema", json.dumps(spec.schema, separators=(",", ":"))]
    if spec.permission_mode:
        command += ["--permission-mode", spec.permission_mode]
    if worktree and not resume:
        command += ["--worktree", worktree]
    return command
