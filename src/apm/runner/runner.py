"""The runner (plans.md §7.1): starts `claude` on a node or on the project, records what it says,
relays its questions and permission prompts, and turns a decomposer's answer into a proposal.

A run is one turn: one `claude` process, from your message to its reply. A conversation is a
chain of runs that share a session, each resuming the one before. A request can chain several
runs by itself: decompose → critique → revise. Every event a run emits is stored, and passed to
`on_event` so a UI can follow along live.

While a run is going, `claude` may need an answer: may it run this command, which of these
options do you want. Those arrive as requests. The run waits until `answer()` is called.
"""

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any

from apm.core.config import ApmConfig, read_config
from apm.core.delivery import Base, choose_base, ensure_pull_request, note_missing_pr
from apm.core.feature_review import (
    ReviewRefused,
    Target,
    check_reviewable,
    conclude,
    find_target,
    prepare_worktree,
    record_review,
    review_subject,
)
from apm.core.feature_review import parse_review as parse_feature_review
from apm.core.files import read_body
from apm.core.ids import handle
from apm.core.lock import CACHE_DIR, pid_alive
from apm.core.model import NodeId, Status
from apm.core.ops import set_status
from apm.core.project import Project, load_graph
from apm.core.proposals import ProposalError, parse_decomposition, parse_review
from apm.core.review_lock import (
    read_review_lock,
    release_review_lock,
    take_review_lock,
    under_review,
)
from apm.core.store import PROJECT, Proposal, Run, RunStatus, Store, TaskReview, content_fingerprint
from apm.core.task_review import implement_blocker
from apm.core.task_verdict import TaskVerdict, VerdictRefused, parse_task_verdict
from apm.runner.agents import (
    AGENTS,
    AgentSpec,
    RunRefused,
    build_command,
    build_prompt,
    check_mode,
    check_startable,
    critic_prompt,
    feature_review_prompt,
    mcp_config,
    revision_prompt,
    worktree_name,
)

FEATURE_REVIEWER = "apm-feature-reviewer"
TASK_REVIEWER = "apm-task-reviewer"

# Kept from the stream: what a transcript needs. Token deltas, rate-limit notices and the like
# are dropped.
KEPT_EVENTS = {"assistant", "user", "result"}
STOP_GRACE_S = 5.0
_LINE_LIMIT = 32 * 1024 * 1024  # one stream-json line can carry a whole file


class RunnerBusy(RunRefused):
    """Too many runs, or one already active here."""

    http_status = 409


type Listener = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class Request:
    """Something a running agent is waiting on the user for."""

    id: str
    run_id: str
    tool: str
    """The tool it wants to use. `AskUserQuestion` is a question; anything else a permission."""
    input: dict[str, Any]
    description: str

    @property
    def kind(self) -> str:
        if self.tool == "AskUserQuestion":
            return "question"
        return "plan" if self.tool == "ExitPlanMode" else "permission"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "kind": self.kind}


class Runner:
    def __init__(self, project: Project, store: Store, on_event: Listener | None = None) -> None:
        self.project = project
        self.store = store
        self._on_event = on_event or (lambda event: None)
        self._tasks: dict[str, asyncio.Task[None]] = {}  # by slot: see `_slot`
        self._slots: dict[str, str] = {}  # run id → slot, for every run of an active pipeline
        self._processes: dict[str, asyncio.subprocess.Process] = {}  # by run id
        self._requests: dict[str, Request] = {}  # by request id
        self._fail_orphans()

    # --- starting ---

    def start(
        self,
        agent: str,
        node_id: NodeId | None,
        message: str = "",
        *,
        critic: bool = True,
        mode: str | None = None,
        branch: str | None = None,
    ) -> Run:
        """Starts `agent` on a node (None = the project as a whole) and returns its run at once;
        the work continues in the background. Must be called from a running event loop.

        `branch` is for the feature reviewer: the branch to review, if APM shouldn't work it out.
        """
        spec = AGENTS.get(agent)
        if spec is None or not spec.user_startable:
            raise RunRefused(f"unknown agent {agent!r}")
        node_id = node_id or PROJECT
        graph = load_graph(self.project)
        if spec.name == FEATURE_REVIEWER and node_id in graph:
            # Started on a feature, it reviews through the feature's review step.
            node_id = review_subject(graph, node_id)[1] or node_id
        # Under review says more than "not reviewed yet", so the lock is checked before the gate.
        locked = spec.name in ("apm-implementer", "apm-decomposer")
        if locked and under_review(self.project.apm_dir, node_id):
            raise RunRefused(f"{handle(node_id)} is under review: wait for its review to finish")
        config = read_config(self.project.apm_dir)
        body = partial(read_body, self.project.apm_dir)
        check_startable(
            spec, graph, node_id, partial(implement_blocker, graph, self.store, config, body=body)
        )
        if spec.name == FEATURE_REVIEWER:
            check_reviewable(graph, node_id, branch)
        mode = check_mode(mode, config)
        prompt = build_prompt(spec, graph, node_id, message)
        run = self._create_run(spec, node_id, prompt, mode=mode, config=config)
        worktree = worktree_name(graph, node_id) if spec.worktree else None
        pipeline = self._pipeline(
            spec, config, run, critic=critic, worktree=worktree, message=message, branch=branch
        )
        if spec.name == TASK_REVIEWER:
            # Held for the pipeline's whole life: `_schedule` releases it however that ends.
            take_review_lock(self.project.apm_dir, node_id, run.id)
        self._schedule(run, pipeline)
        return run

    def continue_run(self, run_id: str, message: str, *, mode: str | None = None) -> Run:
        """Sends the next message of a conversation: a new run that resumes the session of
        `run_id`.
        """
        previous = self.store.get_run(run_id)
        spec = AGENTS[previous.agent]
        if not spec.user_startable:
            raise RunRefused(f"{spec.label} runs on its own: reply in the conversation it reviewed")
        if not message.strip():
            raise RunRefused("the message is empty")
        config = read_config(self.project.apm_dir)
        mode = check_mode(mode or previous.mode or None, config)
        # A conversation whose turns all failed before Claude started (no `claude`, a usage
        # limit) has no session to resume: this message starts it.
        started = self._session_started(run_id)
        typed = message
        if not started:
            graph = load_graph(self.project)
            if previous.node_id and previous.node_id not in graph:
                raise RunRefused("the node this conversation was about no longer exists")
            message = build_prompt(spec, graph, previous.node_id, message)
        run = self._create_run(
            spec,
            previous.node_id,
            message,
            mode=mode,
            config=config,
            # A fresh session gets a fresh id: one that was cut off just as it started may exist.
            session_id=previous.session_id if started else None,
            parent_run=run_id,
        )
        cwd = self._session_cwd(run_id)
        worktree = None
        if not started and spec.worktree:
            worktree = worktree_name(load_graph(self.project), previous.node_id)
        reviewed = self._review_target_of(run_id) if spec.name == FEATURE_REVIEWER else None
        pipeline = self._pipeline(
            spec,
            config,
            run,
            resume=started,
            critic=False,
            cwd=cwd,
            worktree=worktree,
            message=None if started else typed,
            branch=reviewed.get("branch") if reviewed else None,
        )
        self._schedule(run, pipeline)
        return run

    def _session_started(self, run_id: str) -> bool:
        """True if any turn of the conversation up to `run_id` got as far as Claude starting."""
        run: Run | None = self.store.get_run(run_id)
        while run is not None:
            if any(e.data.get("type") == "system" for e in self.store.list_events(run.id)):
                return True
            run = self.store.get_run(run.parent_run) if run.parent_run else None
        return False

    def _slot(self, spec: AgentSpec, node_id: NodeId, conversation: str) -> str:
        """What a run occupies while it is active. Two conversations can go on side by side, but
        one conversation (named by its first run) takes one turn at a time, and a node has one
        decomposer or implementer at a time.
        """
        if spec.name == "apm-assistant":
            return f"chat:{conversation}"
        return f"{spec.name}:{node_id}"

    def _conversation(self, run_id: str) -> str:
        """The first run of the conversation `run_id` belongs to."""
        run = self.store.get_run(run_id)
        seen = {run.id}
        while run.parent_run and run.parent_run not in seen:
            seen.add(run.parent_run)
            run = self.store.get_run(run.parent_run)
        return run.id

    def _create_run(
        self,
        spec: AgentSpec,
        node_id: NodeId,
        prompt: str,
        *,
        mode: str,
        config: ApmConfig | None = None,
        session_id: str | None = None,
        parent_run: str | None = None,
    ) -> Run:
        """Records a run. With `config`, it is the first run of a pipeline, so the limits are
        checked: its slot must be free, and `max_concurrent` pipelines in all.
        """
        if config is not None:
            # A new conversation can't be busy yet; the next turn of one is, while one is going.
            conversation = self._conversation(parent_run) if parent_run else "new"
            slot = self._slot(spec, node_id, conversation)
            elsewhere = any(
                run.agent == spec.name
                and run.pid != os.getpid()
                and run.pid is not None
                and pid_alive(run.pid)
                and (spec.name != "apm-assistant" or self._conversation(run.id) == conversation)
                for run in self.store.list_runs(node_id=node_id, status="running")
            )
            if slot in self._tasks or elsewhere:
                where = "in this conversation" if spec.name == "apm-assistant" else "on this node"
                raise RunnerBusy(f"{spec.label} is already running {where}")
            if len(self._tasks) >= config.runner.max_concurrent:
                limit = config.runner.max_concurrent
                raise RunnerBusy(f"{limit} runs are already active (runner.max_concurrent)")
        run = self.store.create_run(
            node_id=node_id,
            agent=spec.name,
            prompt=prompt,
            session_id=session_id,
            parent_run=parent_run,
            pid=os.getpid(),
            mode=mode,
        )
        self._emit({"type": "run.started", "run": asdict(run)})
        return run

    def _schedule(self, run: Run, pipeline: Coroutine[Any, Any, None]) -> None:
        slot = self._slot(AGENTS[run.agent], run.node_id, self._conversation(run.id))
        task = asyncio.get_running_loop().create_task(pipeline)
        self._tasks[slot] = task
        self._slots[run.id] = slot

        def finished(_: asyncio.Task[None]) -> None:
            self._tasks.pop(slot, None)
            if run.agent == TASK_REVIEWER:
                self._release_review_lock(run)
            for run_id in [r for r, s in self._slots.items() if s == slot]:
                del self._slots[run_id]
            # A pipeline cancelled before its first step never got to record how it ended.
            if self.store.get_run(run.id).status == "running":
                cut_off = self.store.finish_run(run.id, "cancelled")
                self._emit({"type": "run.finished", "run": asdict(cut_off)})

        task.add_done_callback(finished)

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
        message: str | None = None,
        branch: str | None = None,
    ) -> None:
        """`message` is what the user typed, for a run whose first message the runner completes
        once it has started: an implementer's, with the base branch, or a feature reviewer's,
        with the commit it reviews (`branch` names it, else APM works it out).
        """
        implementer = spec.name == "apm-implementer"
        reviewer = spec.name == FEATURE_REVIEWER
        if implementer and message is not None and not resume:
            run = await self._with_base(spec, run, message)
        if reviewer and not resume:
            prepared = await self._review_setup(run, branch, message or "")
            if prepared is None:
                return
            run, cwd = prepared
        if spec.name == TASK_REVIEWER:
            await self._review_task(spec, config, run, resume=resume)
            return
        answer = await self._execute(spec, config, run, resume=resume, worktree=worktree, cwd=cwd)
        if implementer and answer is not None:
            await self._ensure_pr(run)
        if reviewer and answer is not None:
            await self._record_feature_review(run, answer)
            return
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
        unreviewed. It runs in `dontAsk` mode, so it never stops to ask you anything.
        """
        spec = AGENTS["apm-critic"]
        prompt = critic_prompt(load_graph(self.project), parent.node_id, proposal)
        run = self._create_run(spec, parent.node_id, prompt, mode="dontAsk", parent_run=parent.id)
        self._slots[run.id] = self._slots.get(parent.id, "")
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
            mode=first.mode,
            session_id=self.store.get_run(first.id).session_id,
            parent_run=first.id,
        )
        self._slots[run.id] = self._slots.get(first.id, "")
        answer = await self._execute(spec, config, run, resume=True)
        if answer is None:
            return None
        try:
            parse_decomposition(answer)
        except ProposalError:
            return None
        return answer, run

    # --- a feature review ---

    async def _review_setup(
        self, run: Run, branch: str | None, message: str
    ) -> tuple[Run, Path] | None:
        """Works out the commit to review and checks it out (git, so not on the event loop), then
        puts both in the run's first message. If it can't, the run fails and says why.
        """
        try:
            target = await asyncio.to_thread(find_target, self.project, run.node_id, branch)
            cwd = await asyncio.to_thread(prepare_worktree, self.project, run.node_id, target)
        except Exception as err:  # refused, or git failed: the user can fix either
            failed = self.store.finish_run(run.id, "failed", error=str(err))
            self._emit({"type": "run.finished", "run": asdict(failed)})
            return None
        if target.step is not None:
            with contextlib.suppress(Exception):  # the status is a courtesy, not the review
                await asyncio.to_thread(
                    set_status,
                    self.project,
                    target.step,
                    Status.IN_PROGRESS,
                    actor=f"agent:{FEATURE_REVIEWER}",
                    run=run.id,
                )
        graph = load_graph(self.project)
        run = self.store.set_prompt(
            run.id, feature_review_prompt(graph, run.node_id, target, message)
        )
        self._record(run.id, target.as_event())
        self._notice(
            run.id, "info", f"Reviewing `{target.branch}` at `{target.short}`: {target.why}."
        )
        return run, cwd

    def _review_target_of(self, run_id: str) -> dict[str, Any] | None:
        """The commit a review conversation is about, as its first turn recorded it."""
        run: Run | None = self.store.get_run(run_id)
        while run is not None:
            for event in self.store.list_events(run.id):
                if event.data.get("type") == "apm_review_target":
                    return event.data
            run = self.store.get_run(run.parent_run) if run.parent_run else None
        return None

    async def _record_feature_review(self, run: Run, answer: dict[str, Any]) -> None:
        """After every turn of a review: the verdict goes into the feature's description and the
        follow-ups into a proposal, replacing what an earlier turn of this review proposed.
        """
        target = self._review_target_of(run.id)
        if target is None:
            self._notice(
                run.id,
                "warning",
                "APM lost track of which commit this review is about,"
                " so it recorded nothing. Start a new review.",
            )
            return
        try:
            review = parse_feature_review(answer)
        except ReviewRefused as err:
            failed = self.store.finish_run(run.id, "failed", error=str(err))
            self._emit({"type": "run.finished", "run": asdict(failed)})
            return
        conversation = self._conversation(run.id)
        earlier = [
            r.id
            for r in self.store.list_runs(node_id=run.node_id, limit=500)
            if r.id != run.id and r.agent == run.agent and self._conversation(r.id) == conversation
        ]
        try:
            recorded = await asyncio.to_thread(
                record_review,
                self.project,
                self.store,
                run.node_id,
                review,
                branch=target["branch"],
                sha=target["sha"],
                run=run.id,
                supersedes=earlier,
                feature=target.get("feature"),
            )
        except Exception as err:  # the review itself is done: report, don't raise
            self._notice(run.id, "warning", f"APM couldn't record the review: {err}")
            return
        for replaced in recorded.replaced:
            self._emit({"type": "proposal.resolved", "id": replaced, "status": "rejected"})
        concluded = None
        try:
            concluded = await asyncio.to_thread(
                conclude, self.project, Target.from_event(target), review, run=run.id
            )
        except Exception as err:  # the verdict is recorded: report, don't raise
            self._notice(run.id, "warning", f"APM couldn't move the review step on: {err}")
        where = f"`{target['branch']}` at `{target['sha'][:7]}`"
        met = sum(1 for c in review.criteria if c.status == "met")
        if recorded.complete:
            into = target.get("default") or "the default branch"
            text = (
                f"**Complete**: the feature on {where} meets its description ({met} of"
                f" {len(review.criteria)} criteria met)."
            )
            pr = concluded.pr if concluded is not None else None
            if concluded is not None and concluded.landed:
                text += " It is already where it belongs, so the review step is done."
            elif pr is not None and pr.pr:
                text += f" Its pull request into {pr.base}: {pr.pr}"
            elif pr is not None and pr.problem:
                text += f" APM couldn't open its pull request: {pr.problem}."
            else:
                text += f" It is ready to go into {into}."
            self._notice(run.id, "info", text)
        else:
            text = (
                f"**Incomplete**: {met} of {len(review.criteria)} criteria met on {where}. The"
                " verdict is in the review step's description, under Feature review."
            )
            if recorded.accepted:
                count = len(recorded.accepted)
                text += f" APM added {count} follow-up task{'s' if count > 1 else ''} to it."
            elif recorded.proposal is not None:
                count = len(recorded.proposal.data.get("children", []))
                waiting = "task waits" if count == 1 else "tasks wait"
                text += f" {count} follow-up {waiting} under Proposals."
            self._notice(run.id, "warning", text)
        if recorded.proposal is not None:
            self._emit({"type": "proposal.created", "proposal": proposal_dict(recorded.proposal)})

    # --- an implementer's base branch and pull request ---

    async def _with_base(self, spec: AgentSpec, run: Run, message: str) -> Run:
        """Works out the base branch (it fetches, so not on the event loop) and puts it in the
        run's first message.
        """
        try:
            base = await asyncio.to_thread(choose_base, self.project, run.node_id)
        except Exception:  # a base is advice: never a reason not to run
            base = Base(None)
        graph = load_graph(self.project)
        return self.store.set_prompt(
            run.id, build_prompt(spec, graph, run.node_id, message, base=base)
        )

    async def _ensure_pr(self, run: Run) -> None:
        """After an implementer's turn: the node's work should have a pull request. If the agent
        pushed a branch but couldn't open one, APM opens it. If nobody could, the chat says why,
        and so do the node's Notes when a branch is waiting for one. Nothing is marked done.
        """
        try:
            outcome = await asyncio.to_thread(
                ensure_pull_request, self.project, run.node_id, run=run.id
            )
            if outcome.problem and outcome.branch:
                await asyncio.to_thread(
                    note_missing_pr, self.project, run.node_id, outcome, run=run.id
                )
        except Exception as err:  # the run itself is done: report, don't raise
            self._notice(run.id, "warning", f"APM couldn't check for a pull request: {err}")
            return
        if outcome.action == "opened":
            self._notice(
                run.id, "info", f"APM opened the pull request against {outcome.base}: {outcome.pr}"
            )
        elif outcome.action == "found":
            self._notice(
                run.id,
                "info",
                f"APM found the open pull request for {outcome.branch} and recorded it:"
                f" {outcome.pr}",
            )
        elif outcome.problem:
            text = f"No pull request yet: {outcome.problem}."
            if outcome.branch:
                text += (
                    f" Branch `{outcome.branch}` is pushed; the node's Notes say what is missing."
                )
            self._notice(run.id, "warning", text)

    # --- a task review ---

    async def _review_task(self, spec: AgentSpec, config: ApmConfig, run: Run, **kw: Any) -> None:
        """Runs the task reviewer and saves its verdict for the content it was shown: the
        fingerprint is taken before it starts, so an edit made meanwhile gets a review of its own.
        A `too_big` split becomes a proposal, accepted like a decomposer's.
        """
        node = load_graph(self.project)[run.node_id]
        fingerprint = content_fingerprint(node, read_body(self.project.apm_dir, run.node_id))
        answer = await self._execute(spec, config, run, **kw)
        try:
            if answer is None:
                raise VerdictRefused(self.store.get_run(run.id).error or "the run failed")
            verdict = parse_task_verdict(answer)
        except VerdictRefused as err:
            self.store.add_task_review(
                TaskReview(run.node_id, fingerprint, "error", {"error": str(err)}, run.id)
            )
            if answer is not None:
                self.store.finish_run(run.id, "failed", error=str(err))
                self._emit({"type": "run.finished", "run": asdict(self.store.get_run(run.id))})
            self._notice(run.id, "warning", f"APM couldn't record a task verdict: {err}")
            return
        data = verdict.model_dump(mode="json")
        self.store.add_task_review(
            TaskReview(run.node_id, fingerprint, verdict.verdict, data, run.id)
        )
        if verdict.verdict == "too_big":
            split = {"rationale": verdict.summary, "children": data["children"], "warnings": []}
            proposal = self.store.create_proposal(node_id=run.node_id, data=split, run_id=run.id)
            self._emit({"type": "proposal.created", "proposal": proposal_dict(proposal)})
        self._notice(run.id, "warning" if verdict.verdict != "pass" else "info", _said(verdict))

    def _notice(self, run_id: str, level: str, text: str) -> None:
        """A line from APM itself in a run's conversation."""
        self._record(run_id, {"type": "apm_notice", "level": level, "text": text})

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
                mcp_config_path=config_path,
                session_id=run.session_id,
                mode=run.mode,
                resume=resume,
                worktree=worktree,
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd or self.project.code_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_LINE_LIMIT,
                start_new_session=True,  # so stopping it stops its children too
            )
            self._processes[run.id] = process
            stderr_task = asyncio.create_task(_read_tail(process.stderr))
            await self._send(run.id, {"type": "user", "message": _user_message(run.prompt)})
            assert process.stdout is not None
            async for line in process.stdout:
                event = _parse_line(line)
                if event is None:
                    continue
                if event.get("type") == "control_request":
                    await self._on_request(run, event)
                    continue
                if event.get("type") == "result":
                    result = event
                    _close(process)  # nothing more to say: lets the process finish
                if _keep(event):
                    self._record(run.id, event)
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
            for request in [r for r in self._requests.values() if r.run_id == run.id]:
                del self._requests[request.id]
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

    def _record(self, run_id: str, event: dict[str, Any]) -> None:
        stored = self.store.add_event(run_id, event)
        self._emit({"type": "run.event", "run_id": run_id, "event": asdict(stored)})

    async def _send(self, run_id: str, message: dict[str, Any]) -> None:
        process = self._processes.get(run_id)
        if process is None or process.stdin is None or process.stdin.is_closing():
            raise RunRefused("that run is no longer listening")
        process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        with contextlib.suppress(ConnectionError):
            await process.stdin.drain()

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

    # --- questions and permission prompts ---

    async def _on_request(self, run: Run, event: dict[str, Any]) -> None:
        """`claude` asks its host something. Permission prompts and questions wait for the user;
        anything else is acknowledged so the run isn't left hanging.
        """
        body = event.get("request") or {}
        request_id = str(event.get("request_id", ""))
        if body.get("subtype") != "can_use_tool":
            await self._send(run.id, _control_response(request_id, {}))
            return
        tool_input = body.get("input")
        request = Request(
            id=request_id,
            run_id=run.id,
            tool=str(body.get("tool_name", "")),
            input=tool_input if isinstance(tool_input, dict) else {},
            description=str(body.get("description") or ""),
        )
        self._requests[request.id] = request
        self._record(run.id, {"type": "apm_request", **request.as_dict()})
        self._emit({"type": "run.request", "request": request.as_dict()})

    async def answer(
        self,
        request_id: str,
        *,
        allow: bool,
        message: str = "",
        answers: dict[str, str] | None = None,
    ) -> None:
        """Answers a waiting request. For a question, `answers` maps each question's text to the
        chosen option's label (or your own words).
        """
        request = self._requests.pop(request_id, None)
        if request is None:
            raise RunRefused("that request is no longer waiting for an answer")
        if allow:
            updated = {**request.input, "answers": answers} if answers else request.input
            decision = {"behavior": "allow", "updatedInput": updated}
        else:
            decision = {"behavior": "deny", "message": message or "The user said no."}
        await self._send(request.run_id, _control_response(request.id, decision))
        resolved = {
            "type": "apm_request_resolved",
            "id": request.id,
            "allow": allow,
            "message": message,
            "answers": answers or {},
        }
        self._record(request.run_id, resolved)
        self._emit({"type": "run.request_resolved", "run_id": request.run_id, "id": request.id})

    def pending_requests(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self._requests.values() if run_id in (None, r.run_id)]

    # --- stopping ---

    async def cancel(self, run_id: str) -> None:
        """Stops a run, and the rest of its pipeline."""
        task = self._tasks.get(self._slots.get(run_id, ""))
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

    async def wait(self, run_id: str) -> None:
        """Waits for the pipeline a run belongs to, if it is still going."""
        task = self._tasks.get(self._slots.get(run_id, ""))
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def active(self) -> int:
        """How many pipelines are going."""
        return len(self._tasks)

    # --- housekeeping ---

    def _fail_orphans(self) -> None:
        """Runs still marked running whose host process is gone were cut off: say so."""
        for run in self.store.list_runs(status="running", limit=1000):
            if run.pid is None or run.pid == os.getpid() or not pid_alive(run.pid):
                self.store.finish_run(
                    run.id, "failed", error="interrupted: its host process exited"
                )
                if run.agent == TASK_REVIEWER:
                    self._release_review_lock(run, record_error=True)

    def _release_review_lock(self, run: Run, *, record_error: bool = False) -> None:
        """Releases the task's review lock if `run` holds it. `record_error` also stores an
        `error` review of the task's current content, for a run cut off before its verdict.
        """
        apm_dir = self.project.apm_dir
        if (read_review_lock(apm_dir, run.node_id) or {}).get("run") == run.id:
            release_review_lock(apm_dir, run.node_id)
        graph = load_graph(self.project) if record_error else None
        if graph is not None and run.node_id in graph:
            fingerprint = content_fingerprint(graph[run.node_id], read_body(apm_dir, run.node_id))
            data = {"error": "interrupted: its host process exited"}
            self.store.add_task_review(TaskReview(run.node_id, fingerprint, "error", data, run.id))

    def _emit(self, event: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):  # a broken listener must not break a run
            self._on_event(event)


def proposal_dict(proposal: Proposal) -> dict[str, Any]:
    return asdict(proposal)


def _said(verdict: TaskVerdict) -> str:
    """What a task verdict means, for the run's conversation."""
    if verdict.verdict == "pass":
        return f"**Pass**: the task is small enough to build. {verdict.summary}".strip()
    if verdict.verdict == "too_big":
        count = len(verdict.children)
        return (
            f"**Too big**: {verdict.summary} A split into {count} tasks waits under Proposals:"
            " accepting it turns this task into a feature."
        )
    return f"**Unclear**: {verdict.summary} Missing: " + "; ".join(verdict.missing)


def _user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _control_response(request_id: str, response: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": request_id, "response": response},
    }


def _close(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None and not process.stdin.is_closing():
        process.stdin.close()


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
        # A usage limit arrives as an error whose subtype is still "success": the text says it all.
        kind = result.get("subtype") or "error"
        return (detail if kind == "success" else f"{kind}: {detail}").strip(
            ": "
        ) or "the run failed"
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
