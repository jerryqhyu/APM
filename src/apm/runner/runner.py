"""The runner (plans.md §7.1): starts `claude` on a node, records what it says, and turns a
decomposer's answer into a proposal.

A run is one `claude` process. A request can chain several: decompose → critique → revise. Every
event a run emits is stored, and passed to `on_event` so a UI can follow along live.
"""

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from pathlib import Path
from typing import Any

from apm.core.config import ApmConfig, read_config
from apm.core.lock import CACHE_DIR
from apm.core.model import NodeId
from apm.core.project import Project, load_graph
from apm.core.proposals import ProposalError, parse_decomposition, parse_review
from apm.core.store import Proposal, Run, RunStatus, Store
from apm.runner.agents import (
    AGENTS,
    AgentSpec,
    RunRefused,
    build_command,
    build_prompt,
    check_startable,
    critic_prompt,
    mcp_config,
    revision_prompt,
    worktree_name,
)

# Kept from the stream: what a transcript needs. Token deltas, rate-limit notices and the like
# are dropped.
KEPT_EVENTS = {"assistant", "user", "result"}
STOP_GRACE_S = 5.0
_LINE_LIMIT = 32 * 1024 * 1024  # one stream-json line can carry a whole file


class RunnerBusy(RunRefused):
    """Too many runs, or one already active on this node."""


type Listener = Callable[[dict[str, Any]], None]


class Runner:
    def __init__(self, project: Project, store: Store, on_event: Listener | None = None) -> None:
        self.project = project
        self.store = store
        self._on_event = on_event or (lambda event: None)
        self._tasks: dict[NodeId, asyncio.Task[None]] = {}  # one pipeline per node
        self._processes: dict[str, asyncio.subprocess.Process] = {}  # by run id
        self._fail_orphans()

    # --- starting ---

    def start(self, agent: str, node_id: NodeId, guidance: str = "", *, critic: bool = True) -> Run:
        """Starts `agent` on a node and returns its run at once; the work continues in the
        background. Must be called from a running event loop.
        """
        spec = AGENTS.get(agent)
        if spec is None or not spec.user_startable:
            raise RunRefused(f"unknown agent {agent!r}")
        graph = load_graph(self.project)
        check_startable(spec, graph, node_id)
        config = self._claim(node_id)
        prompt = build_prompt(spec, graph, node_id, guidance)
        run = self._create_run(spec, node_id, prompt)
        worktree = worktree_name(graph, node_id) if spec.worktree else None
        self._schedule(run, self._pipeline(spec, config, run, critic=critic, worktree=worktree))
        return run

    def continue_run(self, run_id: str, message: str) -> Run:
        """Sends a follow-up message into a finished run's session, as a new run."""
        previous = self.store.get_run(run_id)
        spec = AGENTS[previous.agent]
        if previous.status == "running":
            raise RunnerBusy("that run is still going")
        if not message.strip():
            raise RunRefused("the message is empty")
        config = self._claim(previous.node_id)
        run = self._create_run(
            spec, previous.node_id, message, session_id=previous.session_id, parent_run=run_id
        )
        cwd = self._session_cwd(run_id)
        self._schedule(run, self._pipeline(spec, config, run, resume=True, critic=False, cwd=cwd))
        return run

    def _claim(self, node_id: NodeId) -> ApmConfig:
        """Checks the limits: one pipeline per node, `max_concurrent` in all."""
        config = read_config(self.project.apm_dir)
        elsewhere = any(
            run.pid != os.getpid() and run.pid is not None and _is_alive(run.pid)
            for run in self.store.list_runs(node_id=node_id, status="running")
        )
        if node_id in self._tasks or elsewhere:
            raise RunnerBusy("a run is already active on this node")
        if len(self._tasks) >= config.runner.max_concurrent:
            limit = config.runner.max_concurrent
            raise RunnerBusy(f"{limit} runs are already active (runner.max_concurrent)")
        return config

    def _schedule(self, run: Run, pipeline: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(pipeline)
        self._tasks[run.node_id] = task

        def finished(_: asyncio.Task[None]) -> None:
            self._tasks.pop(run.node_id, None)
            # A pipeline cancelled before its first step never got to record how it ended.
            if self.store.get_run(run.id).status == "running":
                cut_off = self.store.finish_run(run.id, "cancelled")
                self._emit({"type": "run.finished", "run": asdict(cut_off)})

        task.add_done_callback(finished)

    def _create_run(
        self,
        spec: AgentSpec,
        node_id: NodeId,
        prompt: str,
        *,
        session_id: str | None = None,
        parent_run: str | None = None,
    ) -> Run:
        run = self.store.create_run(
            node_id=node_id,
            agent=spec.name,
            prompt=prompt,
            session_id=session_id,
            parent_run=parent_run,
            pid=os.getpid(),
        )
        self._emit({"type": "run.started", "run": asdict(run)})
        return run

    # --- the pipeline ---

    async def _pipeline(
        self,
        spec: AgentSpec,
        config: ApmConfig,
        run: Run,
        *,
        critic: bool,
        resume: bool = False,
        worktree: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        answer = await self._execute(spec, config, run, resume=resume, worktree=worktree, cwd=cwd)
        if spec.name != "apm-decomposer" or answer is None:
            return
        try:
            parse_decomposition(answer)
        except ProposalError as err:
            self.store.finish_run(run.id, "failed", error=str(err))
            self._emit({"type": "run.finished", "run": asdict(self.store.get_run(run.id))})
            return

        review = None
        if critic:
            review = await self._critique(config, run, answer)
            if review is not None and review.get("verdict") == "revise":
                revised = await self._revise(spec, config, run, review)
                if revised is not None:
                    answer, run = revised
                    review = {**review, "revised": True}
        proposal = self.store.create_proposal(
            node_id=run.node_id, data=answer, run_id=run.id, review=review
        )
        self._emit({"type": "proposal.created", "proposal": proposal_dict(proposal)})

    async def _critique(
        self, config: ApmConfig, parent: Run, proposal: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Runs the critic. A critic that fails doesn't sink the proposal: it just goes
        unreviewed.
        """
        spec = AGENTS["apm-critic"]
        prompt = critic_prompt(load_graph(self.project), parent.node_id, proposal)
        run = self._create_run(spec, parent.node_id, prompt, parent_run=parent.id)
        answer = await self._execute(spec, config, run)
        if answer is None:
            return None
        try:
            return parse_review(answer).model_dump()
        except ProposalError:
            return None

    async def _revise(
        self, spec: AgentSpec, config: ApmConfig, first: Run, review: dict[str, Any]
    ) -> tuple[dict[str, Any], Run] | None:
        """One revision, in the decomposer's own session. If it fails, the first answer stands."""
        run = self._create_run(
            spec,
            first.node_id,
            revision_prompt(review),
            session_id=self.store.get_run(first.id).session_id,
            parent_run=first.id,
        )
        answer = await self._execute(spec, config, run, resume=True)
        if answer is None:
            return None
        try:
            parse_decomposition(answer)
        except ProposalError:
            return None
        return answer, run

    # --- one process ---

    async def _execute(
        self,
        spec: AgentSpec,
        config: ApmConfig,
        run: Run,
        *,
        resume: bool = False,
        worktree: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any] | None:
        """Runs one `claude` process to the end. Returns its structured answer, if it has one and
        the run succeeded.
        """
        status: RunStatus = "failed"
        error: str | None = None
        result: dict[str, Any] = {}
        stderr_task: asyncio.Task[str] | None = None
        try:
            config_path = self._write_mcp_config(spec, run)
            command = build_command(
                spec,
                config,
                prompt=run.prompt,
                mcp_config_path=config_path,
                session_id=run.session_id,
                resume=resume,
                worktree=worktree,
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd or self.project.code_dir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_LINE_LIMIT,
                start_new_session=True,  # so stopping it stops its children too
            )
            self._processes[run.id] = process
            stderr_task = asyncio.create_task(_read_tail(process.stderr))
            assert process.stdout is not None
            async for line in process.stdout:
                event = _parse_line(line)
                if event is None:
                    continue
                if event.get("type") == "result":
                    result = event
                if _keep(event):
                    stored = self.store.add_event(run.id, event)
                    self._emit({"type": "run.event", "run_id": run.id, "event": asdict(stored)})
            code = await process.wait()
            stderr = await stderr_task
            if code == 0 and result and not result.get("is_error"):
                status = "succeeded"
            else:
                error = _failure(result, stderr, code)
        except OSError as err:
            error = f"couldn't start claude: {err}"
        except asyncio.CancelledError:
            status, error = "cancelled", None
            await self._stop(run.id)
            raise
        finally:
            self._processes.pop(run.id, None)
            if stderr_task is not None:
                stderr_task.cancel()
            finished = self.store.finish_run(
                run.id,
                status,
                cost_usd=result.get("total_cost_usd"),
                error=error,
                session_id=result.get("session_id"),
            )
            self._emit({"type": "run.finished", "run": asdict(finished)})
        if status != "succeeded":
            return None
        answer = result.get("structured_output")
        if spec.schema is not None and not isinstance(answer, dict):
            self.store.finish_run(run.id, "failed", error="the agent returned no structured answer")
            self._emit({"type": "run.finished", "run": asdict(self.store.get_run(run.id))})
            return None
        return answer if isinstance(answer, dict) else {}

    def _write_mcp_config(self, spec: AgentSpec, run: Run) -> Path:
        directory = self.project.apm_dir / CACHE_DIR / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run.id}.mcp.json"
        path.write_text(json.dumps(mcp_config(self.project, spec, run.node_id, run.id)))
        return path

    def _session_cwd(self, run_id: str) -> Path | None:
        """Where a run's session lives: an implementer's is in its worktree, and a session can
        only be resumed from the directory it started in.
        """
        run: Run | None = self.store.get_run(run_id)
        while run is not None:
            for event in self.store.list_events(run.id):
                cwd = event.data.get("cwd")
                if event.data.get("type") == "system" and isinstance(cwd, str):
                    return Path(cwd) if Path(cwd).is_dir() else None
            run = self.store.get_run(run.parent_run) if run.parent_run else None
        return None

    # --- stopping ---

    async def cancel(self, run_id: str) -> None:
        """Stops a run, and the rest of its pipeline."""
        task = self._tasks.get(self.store.get_run(run_id).node_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _stop(self, run_id: str) -> None:
        """SIGTERM, then SIGKILL after a grace period, to the whole process group."""
        process = self._processes.get(run_id)
        if process is None or process.returncode is not None:
            return
        _signal_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), STOP_GRACE_S)
        except TimeoutError:
            _signal_group(process.pid, signal.SIGKILL)
            await process.wait()

    async def shutdown(self) -> None:
        """Stops everything: called when the server exits."""
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def wait(self, node_id: NodeId) -> None:
        """Waits for the pipeline on a node to finish, if there is one."""
        task = self._tasks.get(node_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def active_nodes(self) -> list[NodeId]:
        return sorted(self._tasks)

    # --- housekeeping ---

    def _fail_orphans(self) -> None:
        """Runs still marked running whose host process is gone were cut off: say so."""
        for run in self.store.list_runs(status="running", limit=1000):
            if run.pid is None or run.pid == os.getpid() or not _is_alive(run.pid):
                self.store.finish_run(
                    run.id, "failed", error="interrupted: its host process exited"
                )

    def _emit(self, event: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):  # a broken listener must not break a run
            self._on_event(event)


def proposal_dict(proposal: Proposal) -> dict[str, Any]:
    return asdict(proposal)


def _keep(event: dict[str, Any]) -> bool:
    kind = event.get("type")
    return kind in KEPT_EVENTS or (kind == "system" and event.get("subtype") == "init")


def _parse_line(line: bytes) -> dict[str, Any] | None:
    text = line.decode(errors="replace").strip()
    if not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _failure(result: dict[str, Any], stderr: str, code: int) -> str:
    if result.get("is_error"):
        detail = result.get("result") or ", ".join(map(str, result.get("errors") or []))
        return f"{result.get('subtype', 'error')}: {detail}".strip(": ") or "the run failed"
    last = stderr.strip().splitlines()[-1:] or [""]
    return f"claude exited with code {code}" + (f": {last[0]}" if last[0] else "")


async def _read_tail(stream: asyncio.StreamReader | None, limit: int = 8000) -> str:
    """Drains stderr, keeping the end of it for an error message."""
    if stream is None:
        return ""
    tail = b""
    while chunk := await stream.read(65536):
        tail = (tail + chunk)[-limit:]
    return tail.decode(errors="replace")


def _signal_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
