"""The eager pipeline (plans.md §7.9): build → split → verify → record → pull requests.

Implement on a feature that eager mode applies to starts one `apm-eager-builder` run that builds
every To do task on one eager branch. Then APM splits that branch into one commit per task: by
itself when the builder committed task by task (the usual case, since it is told to), otherwise
through an `apm-splitter` run. Either way APM checks the chain (`apm.core.split.check_chain`)
before anything is recorded; a chain that fails is reported in the chat and the feature's Notes,
and nothing is delivered. A good chain is pushed as one branch per task, each task is recorded
and set in progress, and one pull request per task is opened, each targeting the task before it.

A redo runs the same pipeline on the tasks after one whose pull request changed: from that task's
new tip (or the feature branch, once it merged), and it moves their branches forward instead of
replacing them. A split that failed can be tried again on its own ("split again").

All of it is one pipeline in the runner's slot for `apm-eager-builder` on the feature, so it
counts once under `runner.max_concurrent`, like a decomposer and its critic.
"""

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from apm.core.config import ApmConfig, read_config
from apm.core.delivery import (
    GhError,
    PrOutcome,
    ensure_pull_request,
    note_missing_pr,
    read_pr,
    set_pr_base,
)
from apm.core.delivery import add_note as note_line
from apm.core.eager import (
    EagerRefused,
    PrPlan,
    RedoPlan,
    build_blockers,
    build_tasks,
    built_tip,
    code_repo,
    describe_change,
    eager_feature,
    gate_refusal,
    is_running,
    plan_redo,
    record_redo,
    record_split,
    required_before,
    start_round,
    state_of,
    task_branch,
)
from apm.core.errors import ApmError
from apm.core.files import read_body
from apm.core.git import GitError
from apm.core.graph import Graph
from apm.core.ids import handle
from apm.core.model import NodeId, Status
from apm.core.ops import edit_body
from apm.core.project import Project, load_graph
from apm.core.review_lock import under_review
from apm.core.settings import feature_settings
from apm.core.split import (
    Piece,
    SplitAnswer,
    SplitError,
    check_chain,
    mechanical_split,
    parse_split,
    source_lines,
)
from apm.core.store import EagerBuild, Run, RunStatus
from apm.core.views import pr_label
from apm.runner.agents import (
    AGENTS,
    EAGER_BUILDER,
    SPLITTER,
    AgentSpec,
    RunRefused,
    check_mode,
)

if TYPE_CHECKING:
    from apm.runner.runner import Runner

ACTOR = "apm"


class EagerRuns:
    """Starts and runs eager builds, redos and splits for a `Runner`."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    @property
    def project(self) -> Project:
        return self.runner.project

    def _body(self, node_id: NodeId) -> str:
        return read_body(self.project.apm_dir, node_id)

    # --- starting ---

    def start_build(self, feature: NodeId, message: str = "", *, mode: str | None = None) -> Run:
        """Starts the eager build of `feature`, once the gate lets it: every To do task in it
        passes its task review and isn't under review, and nothing it needs from outside the
        build is unfinished. Refuses a feature already split (that is Redo's job).
        """
        runner, store = self.runner, self.runner.store
        graph, config = load_graph(self.project), read_config(self.project.apm_dir)
        if eager_feature(config, graph, feature) != feature:
            raise RunRefused("eager mode is off for this feature: turn it on in its settings")
        if is_running(store, feature):
            raise RunRefused("the eager build of this feature is already running")
        if store.eager_slices(feature):
            raise RunRefused(
                "this feature is already built eagerly and split: use Redo to rebuild after a"
                " change"
            )
        tasks = build_tasks(graph, feature)
        if not tasks:
            raise RunRefused("nothing to build: the feature has no To do tasks")
        self._check_gate(graph, config, feature, tasks)
        mode = check_mode(mode, config)
        spec = AGENTS[EAGER_BUILDER]
        prompt = builder_prompt(graph, config, feature, tasks, message, self._body)
        run = runner._create_run(spec, feature, prompt, mode=mode, config=config)
        runner._schedule(run, self._build(spec, config, run, tasks, message))
        return run

    def start_redo(
        self, feature: NodeId, plan: RedoPlan | None, message: str = "", *, mode: str | None = None
    ) -> Run:
        """Starts what Redo does now: splits a build whose split failed again, or rebuilds the
        tasks after the one `plan` says changed (see `apm.core.eager.plan_redo`).
        """
        runner, store = self.runner, self.runner.store
        graph, config = load_graph(self.project), read_config(self.project.apm_dir)
        graph[feature]  # a feature that is gone: KeyError, as for any run on a deleted node
        build = store.eager_build(feature)
        if build is None:
            raise RunRefused("nothing to redo: this feature hasn't been built eagerly")
        if is_running(store, feature):
            raise RunRefused("the eager build of this feature is already running")
        mode = check_mode(mode, config)
        if state_of(store, build) == "split_failed" and build.sha is not None:
            return self._start_resplit(config, build, mode)
        if plan is None:
            raise RunRefused("nothing to redo: no task pull request has changed since the split")
        if not plan.tasks:
            raise RunRefused("nothing to redo: no open task comes after the one that changed")
        tasks = [t for t in plan.tasks if t in graph]
        spec = AGENTS[EAGER_BUILDER]
        prompt = builder_prompt(graph, config, feature, tasks, message, self._body, plan=plan)
        run = runner._create_run(spec, feature, prompt, mode=mode, config=config)
        runner._schedule(run, self._build(spec, config, run, tasks, message, plan=plan))
        return run

    async def redo(self, feature: NodeId, message: str = "", *, mode: str | None = None) -> Run:
        """Redo, as the button and `apm eager redo` ask for it: works out what changed (it
        fetches and reads pull requests, so off the event loop), then starts it.
        """
        build = self.runner.store.eager_build(feature)
        plan = None
        if build is None or state_of(self.runner.store, build) != "split_failed":
            try:
                plan = await asyncio.to_thread(plan_redo, self.project, self.runner.store, feature)
            except EagerRefused as err:
                raise RunRefused(str(err)) from None
        return self.start_redo(feature, plan, message, mode=mode)

    def _start_resplit(self, config: ApmConfig, build: EagerBuild, mode: str) -> Run:
        spec = AGENTS[SPLITTER]
        prompt = f"Split the eager build on {build.branch} again."
        run = self.runner._create_run(spec, build.feature_id, prompt, mode=mode, config=config)
        self.runner._schedule(run, self._resplit(config, run, build))
        return run

    def _check_gate(
        self, graph: Graph, config: ApmConfig, feature: NodeId, tasks: Sequence[NodeId]
    ) -> None:
        store = self.runner.store
        apm_dir = self.project.apm_dir
        blockers = build_blockers(
            graph,
            store,
            config,
            tasks,
            lambda n: read_body(apm_dir, n),
            lambda n: under_review(apm_dir, n),
        )
        busy = {
            r.node_id for r in store.list_runs(status="running") if r.agent == "apm-implementer"
        }
        blockers += [
            (t, "it is being implemented on its own right now") for t in tasks if t in busy
        ]
        if blockers:
            raise RunRefused(gate_refusal(graph, feature, blockers))

    # --- the pipeline ---

    async def _build(
        self,
        spec: AgentSpec,
        config: ApmConfig,
        run: Run,
        tasks: Sequence[NodeId],
        message: str,
        *,
        plan: RedoPlan | None = None,
    ) -> None:
        runner, store, feature = self.runner, self.runner.store, run.node_id
        try:
            build = await asyncio.to_thread(
                start_round, self.project, store, feature, tasks, run=run.id, redo=plan
            )
        except (ApmError, GitError) as err:
            self._fail_run(run, f"APM couldn't make the eager branch: {err}")
            return
        graph = load_graph(self.project)
        prompt = builder_prompt(
            graph, config, feature, tasks, message, self._body, plan=plan, build=build
        )
        run = store.set_prompt(run.id, prompt)
        count = _count(len(tasks), "task")
        if plan is None:
            what = f"Building {count} eagerly, on `{build.branch}` from {build.base}"
        else:
            change = describe_change(graph, plan.change, build.base)
            what = f"Rebuilding {count} after {change}, on `{build.branch}` from {plan.base_branch}"
        self._notice(run.id, "info", f"{what} at `{build.base_sha[:7]}`.")
        worktree = f"apm-eager-{handle(feature)}-{build.round}"
        answer = await runner._execute(spec, config, run, worktree=worktree)
        if answer is None:
            error = store.get_run(run.id).error or "the run didn't finish"
            store.save_eager_build(replace(build, state="failed", error=error))
            self._notice(
                run.id,
                "warning",
                f"The eager build didn't finish ({error}), so nothing was split. Its branch"
                f" `{build.branch}` is kept."
                + (" Press Redo to try again." if plan is not None else " Implement starts over."),
            )
            return
        await self._split(config, run, build, plan)

    async def _resplit(self, config: ApmConfig, run: Run, build: EagerBuild) -> None:
        """Split again: the same build, on the same base, for the same tasks. A redo round's
        plan is the one it was built for, kept on the build.
        """
        store = self.runner.store
        plan = None
        if build.pending and store.eager_slices(build.feature_id):
            plan = RedoPlan.from_json(build.feature_id, build.pending)
        self._notice(run.id, "info", f"Splitting `{build.branch}` again.")
        build = store.save_eager_build(replace(build, state="splitting", run_id=run.id, error=None))
        await self._split(config, run, build, plan)
        if store.get_run(run.id).status == "running":  # this run has no agent of its own
            after = store.eager_build(build.feature_id)
            if after is not None and after.state == "split":
                self._finish(run, "succeeded")
            else:
                self._finish(run, "failed", after.error if after else "the split failed")

    async def _split(
        self,
        config: ApmConfig,
        run: Run,
        build: EagerBuild,
        plan: RedoPlan | None,
    ) -> None:
        """Splits the finished build, checks the chain, records it and opens the pull requests."""
        store, project = self.runner.store, self.project
        try:
            repo_dir = code_repo(project)
            tip = await asyncio.to_thread(built_tip, repo_dir, build)
        except (ApmError, GitError) as err:
            await self._split_failed(run, build, str(err))
            return
        build = store.save_eager_build(replace(build, sha=tip, state="splitting", error=None))
        graph = load_graph(project)
        tasks = [t for t in build.tasks if t in graph and graph[t].status is not Status.DROPPED]
        before = required_before(graph, tasks)
        made = await asyncio.to_thread(
            mechanical_split, repo_dir, build.base_sha, tip, tasks, before
        )
        warnings: list[str] = []
        last = run
        if isinstance(made, str):
            self._notice(
                run.id,
                "info",
                f"The build isn't one commit per task ({made}), so the splitter carves it up.",
            )
            outcome = await self._run_splitter(config, run, build, tip, tasks)
            if isinstance(outcome, str):
                await self._split_failed(run, build, outcome)
                return
            last, answer = outcome
            pieces = answer.pieces
            warnings += [
                f"{_named(graph, node)}: the splitter couldn't keep its part within the size"
                f" limit{f': {note}' if note else ''}"
                for node, note in answer.over_limit
            ]
        else:
            pieces = made
            self._notice(run.id, "info", "The build is one commit per task: APM split it itself.")
        problems = await asyncio.to_thread(
            check_chain, repo_dir, build.base_sha, tip, pieces, tasks, before
        )
        if problems:
            await self._split_failed(last, build, "; ".join(problems))
            return
        warnings += await asyncio.to_thread(self._size_warnings, config, pieces)
        try:
            if plan is None:
                prs = await asyncio.to_thread(
                    record_split, project, store, build, tip, pieces, run=run.id, warnings=warnings
                )
            else:
                prs = await asyncio.to_thread(
                    record_redo,
                    project,
                    store,
                    build,
                    tip,
                    plan,
                    pieces,
                    run=run.id,
                    warnings=warnings,
                )
        except (ApmError, GitError) as err:
            await self._split_failed(last, build, str(err))
            return
        graph = load_graph(project)
        chain = " → ".join(_named(graph, p.node_id) for p in pieces)
        verb = "Rebuilt and moved forward" if plan is not None else "Split into"
        self._notice(run.id, "info", f"{verb} {_count(len(pieces), 'task')}, in order: {chain}.")
        for warning in warnings:
            self._notice(run.id, "warning", warning)
        await self._pull_requests(run, prs)

    async def _run_splitter(
        self, config: ApmConfig, parent: Run, build: EagerBuild, tip: str, tasks: list[NodeId]
    ) -> tuple[Run, SplitAnswer] | str:
        """Runs the splitter in its own worktree. Its answer, or why there is none."""
        runner = self.runner
        spec = AGENTS[SPLITTER]
        graph = load_graph(self.project)
        prompt = splitter_prompt(graph, config, build, tip, tasks, self._body)
        run = runner._create_run(
            spec, parent.node_id, prompt, mode=parent.mode, parent_run=parent.id
        )
        runner._slots[run.id] = runner._slots.get(parent.id, "")
        worktree = f"apm-split-{handle(parent.node_id)}-{build.round}"
        answer = await runner._execute(spec, config, run, worktree=worktree)
        if answer is None:
            return f"the splitter didn't finish ({runner.store.get_run(run.id).error})"
        try:
            parsed = await asyncio.to_thread(
                parse_split, code_repo(self.project), answer, build.base_sha, tasks
            )
        except SplitError as err:
            return str(err)
        return run, parsed

    def _size_warnings(self, config: ApmConfig, pieces: Sequence[Piece]) -> list[str]:
        """Parts that look bigger than their task's size limit, by APM's rough count."""
        graph = load_graph(self.project)
        repo_dir = code_repo(self.project)
        out = []
        for piece in pieces:
            if piece.node_id not in graph:
                continue
            limit = feature_settings(config, graph, piece.node_id).task_max_lines
            lines = source_lines(repo_dir, piece.parent, piece.commit)
            if lines > limit:
                out.append(
                    f"{_named(graph, piece.node_id)}: its part adds about {lines} lines of source,"
                    f" over its size limit of {limit}"
                )
        return out

    async def _split_failed(self, run: Run, build: EagerBuild, reason: str) -> None:
        """Says so in the chat and the feature's Notes, records nothing as delivered, and keeps
        the eager branch for Redo to split again.
        """
        store = self.runner.store
        store.save_eager_build(replace(build, state="split_failed", error=reason))
        text = (
            f"**The split failed**: {reason}. Nothing was recorded as delivered. The build is kept"
            f" on `{build.branch}`: press Redo to split it again, or fix the tasks first."
        )
        self._notice(run.id, "warning", text)
        line = f"**Eager split failed** (`{build.branch}`): {reason}."
        with contextlib.suppress(ApmError, GitError):
            body = read_body(self.project.apm_dir, build.feature_id)
            if line not in body:
                await asyncio.to_thread(
                    edit_body,
                    self.project,
                    build.feature_id,
                    note_line(body, line),
                    actor=ACTOR,
                    run=run.id,
                )
        self._finish(run, "failed", f"the split failed: {reason}")

    async def _pull_requests(self, run: Run, prs: Sequence[PrPlan]) -> None:
        """Opens each task's pull request against the task before it (or retargets it, after a
        redo), in chain order. One that can't be opened is noted on its task, as after an
        implementer.
        """
        opened: list[str] = []
        for plan in prs:
            try:
                outcome = await asyncio.to_thread(self._one_pr, run, plan)
            except (ApmError, GitError) as err:
                outcome = PrOutcome(problem=str(err))
            graph = load_graph(self.project)
            if outcome.pr:
                opened.append(
                    f"{pr_label(outcome.pr)} {_named(graph, plan.node_id)} into `{plan.base}`"
                )
            else:
                self._notice(
                    run.id,
                    "warning",
                    f"No pull request yet for {_named(graph, plan.node_id)}: {outcome.problem}.",
                )
        if opened:
            self._notice(
                run.id, "info", "Pull requests, in merge order: " + "; ".join(opened) + "."
            )

    def _one_pr(self, run: Run, plan: PrPlan) -> PrOutcome:
        project = self.project
        delivery = load_graph(project)[plan.node_id].delivery
        if delivery is not None and delivery.pr:
            pr = read_pr(code_repo(project), delivery.pr)
            if pr is not None and pr.state == "open" and not pr.merged and pr.base != plan.base:
                try:
                    set_pr_base(code_repo(project), pr, plan.base)
                except GhError as err:
                    return PrOutcome(pr=delivery.pr, problem=f"couldn't retarget it: {err}")
            return PrOutcome(pr=delivery.pr, action="already", base=plan.base)
        body = f"{read_body(project.apm_dir, plan.node_id).strip()}\n\n{plan.body}"
        outcome = ensure_pull_request(project, plan.node_id, run=run.id, body=body, base=plan.base)
        if outcome.problem and outcome.branch:
            note_missing_pr(project, plan.node_id, outcome, run=run.id)
        return outcome

    # --- run bookkeeping ---

    def _notice(self, run_id: str, level: str, text: str) -> None:
        self.runner._notice(run_id, level, text)

    def _finish(self, run: Run, status: RunStatus, error: str | None = None) -> None:
        """Ends a run the pipeline decided the outcome of (a splitter's own run, or a split that
        failed after the agents succeeded), so the chat and `apm implement` see it.
        """
        store = self.runner.store
        finished = store.finish_run(run.id, status, error=error)
        self.runner._emit({"type": "run.finished", "run": asdict(finished)})

    def _fail_run(self, run: Run, error: str) -> None:
        self._notice(run.id, "warning", error)
        self._finish(run, "failed", error)


# --- the agents' first messages ---


type Body = Callable[[NodeId], str]
"""A node's description, e.g. `lambda n: read_body(apm_dir, n)`."""


def builder_prompt(
    graph: Graph,
    config: ApmConfig,
    feature: NodeId,
    tasks: Sequence[NodeId],
    message: str,
    body: Body,
    *,
    plan: RedoPlan | None = None,
    build: EagerBuild | None = None,
) -> str:
    """The builder's first message: the feature, the eager branch, how to commit and what to run
    before handing over, then every task in order with its description and dependencies. A
    redo's starts with what changed.
    """
    node = graph[feature]
    base = build.base if build is not None else "the feature branch"
    if plan is None:
        lines = [
            f'Build feature {handle(feature)} ("{node.title}") of the APM plan eagerly: every task'
            f" below, in one go, on one branch. The feature's full id is {feature}."
        ]
    else:
        changed = _named(graph, plan.change.node_id)
        lines = [
            f'Rebuild part of feature {handle(feature)} ("{node.title}") of the APM plan. APM'
            " built it eagerly and split it into one pull request per task; since then there are"
            f" {describe_change(graph, plan.change, base)}.",
            "The tasks below were built on the old version: build them again on top of what is"
            " there now. Leave the changed task's work as it is.",
            "",
            f"How {changed} differs from what APM split for it:",
            "",
            "```diff",
            plan.diff.rstrip() or "(no difference in content)",
            "```",
        ]
    lines.append("")
    if build is not None:
        start = build.base if plan is None else plan.base_branch
        lines.append(
            f"Work on branch {build.branch}. APM made it from {start} at {build.base_sha[:12]}"
            f" and pushed it: run `git fetch origin {build.branch}` and"
            f" `git checkout -B {build.branch} origin/{build.branch}`."
        )
    lines += [
        "Build the tasks in the order below. Commit each task on its own, in that order, and end"
        " every commit message with the trailer `APM-Node: <that task's full id>`: APM splits"
        " the branch into one branch and one pull request per task by those trailers. Never mix"
        " two tasks in one commit.",
        "Before you hand over, run the repository's tests and linters (its CLAUDE.md, README or"
        " CI configuration says which) and fix what fails. Don't hand over work with failing"
        " checks: if you can't make them pass, say exactly what fails.",
        "Then push the branch"
        + (f" (`git push origin {build.branch}`)" if build is not None else "")
        + ". Don't open pull requests and don't push any other branch: APM checks the split,"
        " then pushes the task branches and opens the pull requests itself.",
        "",
        "The tasks, in the order to build them:",
    ]
    inside = set(tasks)
    for number, task in enumerate(tasks, start=1):
        deps = graph.dependencies(task)
        needs = [_named(graph, d) for d in deps if d in inside]
        done = [_named(graph, d) for d in deps if d not in inside]
        limit = feature_settings(config, graph, task).task_max_lines
        lines += [
            "",
            f"### {number}. {handle(task)} {graph[task].title}",
            f"Full id: {task}. Its branch, once split: {task_branch(graph, task)}.",
            "Depends on: "
            + ("; ".join(needs) if needs else "nothing in this build")
            + (f". Already done: {'; '.join(done)}" if done else ""),
            f"Size limit: {limit} real lines of code (tests and docs come on top).",
            "",
            body(task).strip() or "(no description)",
        ]
    text = "\n".join(lines)
    return text + (f"\n\nGuidance from the user:\n{message.strip()}" if message.strip() else "")


def splitter_prompt(
    graph: Graph,
    config: ApmConfig,
    build: EagerBuild,
    tip: str,
    tasks: Sequence[NodeId],
    body: Body,
) -> str:
    """The splitter's first message: the build, the base, and each task's place in the chain."""
    feature = graph[build.feature_id]
    lines = [
        f'Split the eager build of feature {handle(feature.id)} ("{feature.title}") of the APM'
        " plan into one commit per task.",
        "",
        f"The build is on {build.branch} at {tip}. It starts from {build.base_sha}:"
        f" `git diff {build.base_sha[:12]} {tip[:12]}` is everything it changed.",
        "Make a chain of commits, one per task, in the order below: the first on"
        f" {build.base_sha}, each next one on the one before. Each holds only its task's part of"
        " the build, and its message ends with the trailer `APM-Node: <the task's full id>`."
        " The last commit's tree must be exactly the build's: `git diff --quiet <last commit>"
        f" {tip[:12]}` must succeed.",
        "Don't push and don't open pull requests: APM checks your chain, then pushes the task"
        " branches and opens them.",
        "",
        "The tasks, in chain order:",
    ]
    for number, task in enumerate(tasks, start=1):
        limit = feature_settings(config, graph, task).task_max_lines
        lines += [
            "",
            f"{number}. {handle(task)} {graph[task].title}: full id {task}, branch"
            f" {task_branch(graph, task)}, size limit {limit} real lines.",
            body(task).strip() or "(no description)",
        ]
    lines += ["", "Return the chain."]
    return "\n".join(lines)


def _named(graph: Graph, node_id: NodeId) -> str:
    title = graph[node_id].title if node_id in graph else "a deleted task"
    return f'{handle(node_id)} ("{title}")'


def _count(n: int, one: str) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {one}s"
