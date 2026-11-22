"""Eager mode end to end, with a fake `claude`, real git and a fake `gh`: a feature is built on one
branch, split into one branch and one pull request per task, chained so each shows only its own
part, and rebuilt after a task's pull request changes.

The fake `claude` writes no code, so the tests do what the agents would: commit to the eager
branch for the builder, and make the chain for the splitter (see `Agents`).
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from apm.core.delivery import feature_branch
from apm.core.eager import plan_redo
from apm.core.files import read_body
from apm.core.git import CommitMessage, git
from apm.core.ids import handle
from apm.core.model import NodeId, Status
from apm.core.ops import NewNode, create_nodes, decompose
from apm.core.project import Project, load_graph
from apm.core.reconcile import reconcile
from apm.core.split import commit_tree, tree_of
from apm.core.store import EagerBuild
from apm.runner.agents import EAGER_BUILDER, SPLITTER, RunRefused
from apm.runner.auto import AutoQueue
from apm.runner.feature_runs import IMPLEMENTER, implement_feature
from apm.runner.queue import RunQueue
from apm.runner.runner import Runner
from tests.conftest import CodeRepo
from tests.fake_gh import FakeGh
from tests.runner.fake import FakeClaude
from tests.runner.test_runner import Harness

EAGER_ON = "features:\n  eager: true\n"


def configure(project: Project, text: str) -> None:
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + text)


def git_out(repo: CodeRepo, *args: str) -> str:
    return git(repo.dir, list(args)).strip()


class Agents:
    """Stands in for the eager builder and the splitter: when the pipeline runs one of them, the
    git work it would do happens first, then the fake `claude` answers.
    """

    def __init__(self, repo: CodeRepo, runner: Runner, tmp_path: Path, monkeypatch: Any) -> None:
        self.repo, self.runner = repo, runner
        self.build: Callable[[EagerBuild], object] | None = None
        self.split: Callable[[EagerBuild], list[dict[str, Any]]] | None = None
        self.answer = tmp_path / "split.json"
        monkeypatch.setenv("FAKE_CLAUDE_SPLIT", str(self.answer))
        real = runner._execute

        async def execute(spec: Any, config: Any, run: Any, **kwargs: Any) -> Any:
            build = runner.store.eager_build(run.node_id)
            if spec.name == EAGER_BUILDER and self.build is not None and build is not None:
                await asyncio.to_thread(self.build, build)
            if spec.name == SPLITTER and self.split is not None and build is not None:
                tasks = await asyncio.to_thread(self.split, build)
                self.answer.write_text(json.dumps({"summary": "Split.", "tasks": tasks}))
            return await real(spec, config, run, **kwargs)

        runner._execute = execute  # ty: ignore[invalid-assignment]

    def commit_on(self, build: EagerBuild, commits: Sequence[tuple[str, str, str | None]]) -> str:
        """Commits `(file, content, task)` in order on the eager branch and pushes it, as the
        builder does. `task` None leaves the trailer out.
        """
        repo = self.repo
        git(repo.dir, ["fetch", "--quiet", "origin"])
        git(repo.dir, ["checkout", "--quiet", "-B", "builder-work", f"origin/{build.branch}"])
        for name, content, task in commits:
            path = repo.dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            git(repo.dir, ["add", name])
            message = f"Build {name}" + (f"\n\nAPM-Node: {task}" if task else "")
            git(repo.dir, ["commit", "--quiet", "-m", message])
        git(repo.dir, ["push", "--quiet", "origin", f"HEAD:refs/heads/{build.branch}"])
        tip = git_out(repo, "rev-parse", "HEAD")
        git(repo.dir, ["checkout", "--quiet", "main"])
        return tip

    def chain(
        self, build: EagerBuild, parts: Sequence[tuple[NodeId, Sequence[str]]], tip: str
    ) -> list[dict[str, Any]]:
        """Makes the chain a splitter would, with real git: each task's files as they are at
        `tip`, on top of the one before. Returns its answer's tasks.
        """
        repo, out, parent = self.repo, [], build.base_sha
        for task, files in parts:
            git(repo.dir, ["read-tree", parent])
            for name in files:
                blob = git_out(repo, "rev-parse", f"{tip}:{name}")
                git(repo.dir, ["update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"])
            tree = git_out(repo, "write-tree")
            message = CommitMessage(subject=f"Part of {task[-6:]}", trailers=[("APM-Node", task)])
            parent = commit_tree(repo.dir, tree, [parent], message)
            out.append(
                {
                    "node_id": task,
                    "branch": f"split/{task[-6:]}",
                    "commit": parent,
                    "within_limit": True,
                    "note": "",
                }
            )
        git(repo.dir, ["read-tree", "HEAD"])
        return out


@pytest.fixture
def eager(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh, tmp_path: Path, monkeypatch: Any
) -> tuple[Harness, Agents]:
    configure(code_repo.project, EAGER_ON)
    fake_gh.numbered()
    harness = Harness(code_repo.project)
    return harness, Agents(code_repo, harness.runner, tmp_path, monkeypatch)


def three_tasks(project: Project, harness: Harness) -> tuple[NodeId, list[NodeId]]:
    """A feature with three reviewed tasks, each needing the one before."""
    feature = create_nodes(project, [NewNode(title="Search")]).created[0]
    tasks = list(
        decompose(
            project,
            feature,
            [
                NewNode(title="Parse", key="p"),
                NewNode(title="Index", key="i", depends_on_keys=["p"]),
                NewNode(title="Rank", key="r", depends_on_keys=["i"]),
            ],
        ).created
    )
    for task in tasks:
        harness.review(task)
    return feature, tasks


def per_task(agents: Agents, tasks: Sequence[NodeId]) -> Callable[[EagerBuild], None]:
    """A builder that commits one file per task, each with its trailer."""

    def build(b: EagerBuild) -> None:
        agents.commit_on(b, [(f"src/{t[-6:]}.py", f"# {t}\nx = 1\n", t) for t in b.tasks])

    return build


async def settle(harness: Harness) -> None:
    for _ in range(3):
        for run in harness.store.list_runs(status="running"):
            await harness.runner.wait(run.id)


def notices(harness: Harness, run_id: str) -> list[tuple[str, str]]:
    return [
        (e.data["level"], e.data["text"])
        for e in harness.store.list_events(run_id)
        if e.data.get("type") == "apm_notice"
    ]


def posted(fake_gh: FakeGh) -> list[tuple[str, str]]:
    """(head, base) of each pull request opened, in order."""
    calls = [c for c in fake_gh.calls if c[:3] == ["api", "-X", "POST"]]
    return [(fake_gh.field(c, "head"), fake_gh.field(c, "base")) for c in calls]


# --- build → split → verify → pull requests ---


async def test_a_feature_is_built_split_and_opened_as_a_chain_of_pull_requests(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh, fake_claude: FakeClaude
) -> None:
    harness, agents = eager
    project = code_repo.project
    feature, tasks = three_tasks(project, harness)
    agents.build = per_task(agents, tasks)

    batch = implement_feature(RunQueue(harness.runner), feature)
    assert [(r.agent, r.node_id) for r in batch.started] == [(EAGER_BUILDER, feature)]
    assert batch.skipped == ()
    await settle(harness)

    build = harness.store.eager_build(feature)
    assert build is not None
    assert (build.state, build.branch, build.round) == (
        "split",
        f"eager/{handle(feature)}-search",
        1,
    )
    graph = load_graph(project)
    home = feature_branch(graph, feature)
    assert build.base == home
    slices = harness.store.eager_slices(feature)
    assert [s.node_id for s in slices] == tasks  # dependency order

    # One commit per task, each on the one before, the last one holding the whole build.
    parent = build.base_sha
    for s in slices:
        assert git_out(code_repo, "rev-parse", f"{s.commit}^") == parent
        assert git_out(code_repo, "rev-parse", f"origin/{s.branch}") == s.commit
        assert s.branch == f"apm/{handle(s.node_id)}-{graph[s.node_id].title.lower()}"
        parent = s.commit
    assert build.sha is not None
    assert tree_of(code_repo.dir, slices[-1].commit) == tree_of(code_repo.dir, build.sha)

    # APM records each delivery, sets it in progress, and opens one PR per task, chained.
    for number, s in enumerate(slices, start=101):
        node = graph[s.node_id]
        assert node.status is Status.IN_PROGRESS
        assert node.delivery is not None
        assert (node.delivery.branch, node.delivery.commits) == (s.branch, (s.commit,))
        assert node.delivery.pr == f"https://github.com/o/r/pull/{number}"
    assert posted(fake_gh) == [
        (slices[0].branch, home),
        (slices[1].branch, slices[0].branch),
        (slices[2].branch, slices[1].branch),
    ]
    (run,) = harness.store.list_runs()
    said = [text for _, text in notices(harness, run.id)]
    assert any("APM split it itself" in text for text in said)
    assert "#101" in said[-1]
    assert "#103" in said[-1]
    (call,) = fake_claude.calls  # the builder only: no splitter needed
    assert call["agent"] == EAGER_BUILDER
    for task in tasks:
        assert task in call["prompt"]
    assert "run the repository's tests and linters" in call["prompt"]
    assert "--worktree" in call["argv"]

    # Merging in order: reconcile marks each done and moves the next PR onto the feature branch.
    for number, s in enumerate(slices, start=101):
        git(code_repo.dir, ["fetch", "--quiet", "origin"])
        git(code_repo.dir, ["checkout", "--quiet", "-B", "merging", f"origin/{home}"])
        git(code_repo.dir, ["merge", "--quiet", "--no-ff", "-m", f"Merge #{number}", s.commit])
        git(code_repo.dir, ["push", "--quiet", "origin", f"HEAD:refs/heads/{home}"])
        fake_gh.merge(number, git_out(code_repo, "rev-parse", "HEAD"))
        git(code_repo.dir, ["checkout", "--quiet", "main"])
        reconcile(project, retarget=True)
        assert load_graph(project)[s.node_id].status is Status.DONE
        if number < 103:
            assert fake_gh.pr(number + 1)["base"]["ref"] == home
    # Each merged exactly as split: no redo is due.
    assert plan_redo(project, harness.store, feature) is None
    assert all(s.landed for s in harness.store.eager_slices(feature))


async def test_a_build_that_isnt_one_commit_per_task_goes_to_the_splitter(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh, fake_claude: FakeClaude
) -> None:
    harness, agents = eager
    project = code_repo.project
    feature, tasks = three_tasks(project, harness)
    files = {t: f"src/{t[-6:]}.py" for t in tasks}
    agents.build = lambda b: agents.commit_on(b, [(f, "x = 1\n", None) for f in files.values()])
    agents.split = lambda b: agents.chain(
        b, [(t, [files[t]]) for t in tasks], git_out(code_repo, "rev-parse", f"origin/{b.branch}")
    )

    implement_feature(RunQueue(harness.runner), feature)
    await settle(harness)

    build = harness.store.eager_build(feature)
    assert build is not None
    assert build.state == "split"
    assert [c["agent"] for c in fake_claude.calls] == [EAGER_BUILDER, SPLITTER]
    splitter = fake_claude.calls[1]
    assert build.sha is not None
    assert build.sha in splitter["prompt"]
    assert all(t in splitter["prompt"] for t in tasks)
    runs = sorted(harness.store.list_runs(), key=lambda r: r.started)
    assert [(r.agent, r.status) for r in runs] == [
        (EAGER_BUILDER, "succeeded"),
        (SPLITTER, "succeeded"),
    ]
    assert runs[1].parent_run == runs[0].id
    assert [s.node_id for s in harness.store.eager_slices(feature)] == tasks
    assert len(posted(fake_gh)) == 3


async def test_a_split_whose_trees_dont_match_is_refused_and_nothing_is_delivered(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh
) -> None:
    harness, agents = eager
    project = code_repo.project
    feature, tasks = three_tasks(project, harness)
    files = {t: f"src/{t[-6:]}.py" for t in tasks}
    agents.build = lambda b: agents.commit_on(b, [(f, "x = 1\n", None) for f in files.values()])
    # The splitter loses the last task's file: the chain doesn't end at the build's tree.
    agents.split = lambda b: agents.chain(
        b,
        [(tasks[0], [files[tasks[0]]]), (tasks[1], [files[tasks[1]]]), (tasks[2], [])],
        git_out(code_repo, "rev-parse", f"origin/{b.branch}"),
    )

    implement_feature(RunQueue(harness.runner), feature)
    await settle(harness)

    build = harness.store.eager_build(feature)
    assert build is not None
    assert build.state == "split_failed"
    assert build.error is not None
    assert "changes nothing" in build.error
    assert harness.store.eager_slices(feature) == []
    graph = load_graph(project)
    assert all(graph[t].status is Status.TODO and graph[t].delivery is None for t in tasks)
    assert posted(fake_gh) == []
    remote = git_out(code_repo, "ls-remote", "origin", "refs/heads/apm/*")
    assert remote == ""  # no task branch was pushed
    assert "Eager split failed" in read_body(project.apm_dir, feature)
    splitter = next(r for r in harness.store.list_runs() if r.agent == SPLITTER)
    assert splitter.status == "failed"
    assert any("The split failed" in text for _, text in notices(harness, splitter.id))


# --- the gate, and Implement on one task ---


def test_the_gate_refuses_a_build_while_a_task_hasnt_passed_review(
    code_repo: CodeRepo, eager: tuple[Harness, Agents]
) -> None:
    harness, _ = eager
    project = code_repo.project
    feature, tasks = three_tasks(project, harness)
    harness.review(tasks[1], "too_big")
    batch = implement_feature(RunQueue(harness.runner), feature)
    assert batch.started == ()
    reasons = {s.node_id: s.reason for s in batch.skipped}
    assert set(reasons) == set(tasks)
    assert "holds back the eager build" in reasons[tasks[1]]
    assert "too_big" in reasons[tasks[1]]
    assert "waits for the eager build" in reasons[tasks[0]]
    with pytest.raises(RunRefused, match=r"can't start until every task in it is ready"):
        harness.runner.start(IMPLEMENTER, tasks[0])
    assert harness.store.list_runs() == []
    assert harness.store.eager_build(feature) is None


async def test_implement_on_one_task_builds_the_feature_then_refuses_once_split(
    code_repo: CodeRepo, eager: tuple[Harness, Agents]
) -> None:
    harness, agents = eager
    project = code_repo.project
    feature, tasks = three_tasks(project, harness)
    agents.build = per_task(agents, tasks)

    run = harness.runner.start(IMPLEMENTER, tasks[2])  # blocked on its own: the build covers it
    assert (run.agent, run.node_id) == (EAGER_BUILDER, feature)
    with pytest.raises(RunRefused, match="eager build of its feature is running"):
        harness.runner.start(IMPLEMENTER, tasks[0])
    await settle(harness)

    with pytest.raises(RunRefused, match="its tasks come from the split; use Redo"):
        harness.runner.start(IMPLEMENTER, tasks[0])
    with pytest.raises(RunRefused, match="already built eagerly and split"):
        harness.runner.start(EAGER_BUILDER, feature)
    # A task added after the split isn't part of it: it is built on its own.
    (later,) = decompose(project, feature, [NewNode(title="Later")]).created
    harness.review(later)
    run = harness.runner.start(IMPLEMENTER, later)
    assert (run.agent, run.node_id) == (IMPLEMENTER, later)
    await settle(harness)


async def test_eager_off_changes_nothing(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    harness = Harness(code_repo.project)
    feature, tasks = three_tasks(code_repo.project, harness)
    batch = implement_feature(RunQueue(harness.runner), feature)
    assert [(r.agent, r.node_id) for r in batch.started] == [(IMPLEMENTER, tasks[0])]
    reasons = [s.reason for s in batch.skipped]
    assert all(r.startswith("blocked") for r in reasons)
    await settle(harness)
    assert harness.store.eager_builds() == []
    assert all(c["agent"] == IMPLEMENTER for c in fake_claude.calls)


# --- redo: rebuilding the tasks after one that changed ---


async def split_three(
    code_repo: CodeRepo, harness: Harness, agents: Agents
) -> tuple[NodeId, list[NodeId]]:
    """A feature built eagerly and split into a chain of three pull requests (#101-#103)."""
    feature, tasks = three_tasks(code_repo.project, harness)
    agents.build = per_task(agents, tasks)
    harness.runner.start(EAGER_BUILDER, feature)
    await settle(harness)
    return feature, tasks


def push_on(repo: CodeRepo, branch: str, name: str, content: str, message: str) -> str:
    """Someone pushes a commit to `branch` (on `origin`): a review fix, say."""
    git(repo.dir, ["fetch", "--quiet", "origin"])
    git(repo.dir, ["checkout", "--quiet", "-B", "someone", f"origin/{branch}"])
    path = repo.dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo.dir, ["add", name])
    git(repo.dir, ["commit", "--quiet", "-m", message])
    git(repo.dir, ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"])
    sha = git_out(repo, "rev-parse", "HEAD")
    git(repo.dir, ["checkout", "--quiet", "main"])
    return sha


def rebuild_per_task(agents: Agents) -> Callable[[EagerBuild], object]:
    """A redo's builder: commits each task it is given again, with new content."""
    return lambda b: agents.commit_on(
        b, [(f"src/{t[-6:]}.py", f"# {t}\nx = 2  # rebuilt\n", t) for t in b.tasks]
    )


async def test_redo_rebuilds_only_the_tasks_after_a_change_and_never_rewrites_a_branch(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh, fake_claude: FakeClaude
) -> None:
    harness, agents = eager
    project = code_repo.project
    feature, (parse, index, rank) = await split_three(code_repo, harness, agents)
    old = {s.node_id: s for s in harness.store.eager_slices(feature)}
    fix = push_on(code_repo, old[parse].branch, "src/fix.py", "fixed = True\n", "Review fix")

    plan = plan_redo(project, harness.store, feature)
    assert plan is not None
    assert (plan.change.node_id, plan.change.kind, plan.change.tip) == (parse, "pushed", fix)
    assert (plan.base, plan.base_branch, plan.tasks) == (fix, old[parse].branch, (index, rank))
    assert "src/fix.py" in plan.diff

    agents.build = rebuild_per_task(agents)
    run = await harness.runner.eager.redo(feature)
    assert (run.agent, run.node_id) == (EAGER_BUILDER, feature)
    await settle(harness)

    build = harness.store.eager_build(feature)
    assert build is not None
    assert (build.state, build.round, build.base_sha) == ("split", 2, fix)
    assert build.branch.endswith("-2")
    prompt = fake_claude.calls[-1]["prompt"]
    assert prompt.startswith(f"Rebuild part of feature {handle(feature)}")
    assert "fixed = True" in prompt  # what changed upstream
    assert parse not in prompt.split("The tasks, in the order to build them:")[1]

    new = {s.node_id: s for s in harness.store.eager_slices(feature)}
    assert git_out(code_repo, "rev-parse", f"origin/{old[parse].branch}") == fix  # untouched
    assert new[parse].commit == fix  # the change is accepted as the task's new state
    below = fix
    for task in (index, rank):
        tip = git_out(code_repo, "rev-parse", f"origin/{old[task].branch}")
        assert tip == new[task].commit
        # Moved forward, never rewritten: the old tip and the task before it are its parents.
        assert git_out(code_repo, "rev-list", "--parents", "-n", "1", tip).split()[1:] == [
            old[task].commit,
            below,
        ]
        assert "x = 2  # rebuilt" in git_out(code_repo, "show", f"{tip}:src/{task[-6:]}.py")
        delivery = load_graph(project)[task].delivery
        assert delivery is not None
        assert delivery.commits == (old[task].commit, tip)
        below = tip
    assert tree_of(code_repo.dir, new[rank].commit) == tree_of(code_repo.dir, build.sha or "")
    # The pull requests stay where they were, chained: no new ones.
    assert len(posted(fake_gh)) == 3
    assert fake_gh.pr(102)["base"]["ref"] == old[parse].branch
    assert fake_gh.pr(103)["base"]["ref"] == old[index].branch
    assert plan_redo(project, harness.store, feature) is None


async def test_redo_after_a_merge_with_changes_starts_from_the_feature_branch(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh
) -> None:
    harness, agents = eager
    project = code_repo.project
    feature, (parse, index, rank) = await split_three(code_repo, harness, agents)
    old = {s.node_id: s for s in harness.store.eager_slices(feature)}
    build = harness.store.eager_build(feature)
    assert build is not None
    # #101 is squash-merged with an edit made during review.
    git(code_repo.dir, ["checkout", "--quiet", "-B", "squash", f"origin/{build.base}"])
    git(code_repo.dir, ["checkout", "--quiet", old[parse].commit, "--", "."])
    (code_repo.dir / "src" / "edited.py").write_text("edited = True\n")
    git(code_repo.dir, ["add", "-A"])
    git(code_repo.dir, ["commit", "--quiet", "-m", "Parse (#101)"])
    git(code_repo.dir, ["push", "--quiet", "origin", f"HEAD:refs/heads/{build.base}"])
    squashed = git_out(code_repo, "rev-parse", "HEAD")
    git(code_repo.dir, ["checkout", "--quiet", "main"])
    fake_gh.merge(101, squashed)

    plan = plan_redo(project, harness.store, feature)
    assert plan is not None
    assert (plan.change.kind, plan.base, plan.base_branch) == ("merged", squashed, build.base)
    assert plan.tasks == (index, rank)
    agents.build = rebuild_per_task(agents)
    await harness.runner.eager.redo(feature)
    await settle(harness)

    assert git_out(code_repo, "rev-parse", f"origin/{old[parse].branch}") == old[parse].commit
    tip = git_out(code_repo, "rev-parse", f"origin/{old[index].branch}")
    assert git_out(code_repo, "rev-list", "--parents", "-n", "1", tip).split()[1:] == [
        old[index].commit,
        squashed,
    ]
    assert fake_gh.pr(102)["base"]["ref"] == build.base  # now on the feature branch
    assert fake_gh.pr(103)["base"]["ref"] == old[index].branch
    slices = {s.node_id: s for s in harness.store.eager_slices(feature)}
    assert slices[parse].landed == squashed


async def test_redo_refuses_to_overwrite_commits_apm_didnt_make(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh
) -> None:
    harness, agents = eager
    feature, (parse, index, _) = await split_three(code_repo, harness, agents)
    old = {s.node_id: s for s in harness.store.eager_slices(feature)}
    push_on(code_repo, old[parse].branch, "src/fix.py", "fixed = True\n", "Review fix")
    theirs = push_on(code_repo, old[index].branch, "src/theirs.py", "mine = 1\n", "Someone's")

    with pytest.raises(RunRefused, match="APM won't rebuild over that"):
        await harness.runner.eager.redo(feature)
    assert git_out(code_repo, "rev-parse", f"origin/{old[index].branch}") == theirs
    build = harness.store.eager_build(feature)
    assert build is not None
    assert build.round == 1


async def test_redo_refuses_when_a_task_after_the_change_has_merged(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh
) -> None:
    harness, agents = eager
    feature, (parse, _, rank) = await split_three(code_repo, harness, agents)
    old = {s.node_id: s for s in harness.store.eager_slices(feature)}
    fake_gh.merge(103, old[rank].commit)  # merged into the task before it
    push_on(code_repo, old[parse].branch, "src/fix.py", "fixed = True\n", "Review fix")
    with pytest.raises(RunRefused, match="never touches merged work"):
        await harness.runner.eager.redo(feature)
    assert git_out(code_repo, "rev-parse", f"origin/{old[rank].branch}") == old[rank].commit


async def test_a_task_merged_as_split_is_left_alone_when_a_later_one_changes(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh
) -> None:
    harness, agents = eager
    project = code_repo.project
    feature, (parse, index, rank) = await split_three(code_repo, harness, agents)
    old = {s.node_id: s for s in harness.store.eager_slices(feature)}
    build = harness.store.eager_build(feature)
    assert build is not None
    # #101 lands as split (a fast-forward of the feature branch), then #102 gets a review fix.
    landed = f"{old[parse].commit}:refs/heads/{build.base}"
    git(code_repo.dir, ["push", "--quiet", "origin", landed])
    fake_gh.merge(101, old[parse].commit)
    reconcile(project, retarget=True)
    assert load_graph(project)[parse].status is Status.DONE
    assert fake_gh.pr(102)["base"]["ref"] == build.base
    fix = push_on(code_repo, old[index].branch, "src/fix.py", "fixed = True\n", "Review fix")

    plan = plan_redo(project, harness.store, feature)
    assert plan is not None
    assert (plan.change.node_id, plan.tasks) == (index, (rank,))
    agents.build = rebuild_per_task(agents)
    await harness.runner.eager.redo(feature)
    await settle(harness)

    assert git_out(code_repo, "rev-parse", f"origin/{old[parse].branch}") == old[parse].commit
    assert git_out(code_repo, "rev-parse", f"origin/{old[index].branch}") == fix
    rebuilt = git_out(code_repo, "rev-parse", f"origin/{old[rank].branch}")
    assert git_out(code_repo, "rev-list", "--parents", "-n", "1", rebuilt).split()[1:] == [
        old[rank].commit,
        fix,
    ]
    slices = {s.node_id: s for s in harness.store.eager_slices(feature)}
    assert slices[parse].landed == old[parse].commit
    assert load_graph(project)[parse].status is Status.DONE


# --- redo in `apm serve`: after each reconcile, with `automation.eager_redo` ---


async def test_apm_serve_redoes_a_changed_task_on_its_own(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh
) -> None:
    harness, agents = eager
    feature, (parse, index, rank) = await split_three(code_repo, harness, agents)
    old = {s.node_id: s for s in harness.store.eager_slices(feature)}
    fix = push_on(code_repo, old[parse].branch, "src/fix.py", "fixed = True\n", "Review fix")
    agents.build = rebuild_per_task(agents)
    queue = AutoQueue(harness.runner, debounce_s=0)

    assert queue.reconcile()
    pending = harness.store.eager_build(feature).pending  # ty: ignore[possibly-missing-attribute]
    assert pending is not None
    assert (pending["node_id"], pending["tasks"]) == (parse, [index, rank])
    queue.scan()
    queue.pump()
    await settle(harness)

    build = harness.store.eager_build(feature)
    assert build is not None
    assert (build.state, build.round, build.pending) == ("split", 2, None)
    tip = git_out(code_repo, "rev-parse", f"origin/{old[rank].branch}")
    assert tip != old[rank].commit
    assert harness.store.eager_slices(feature)[0].commit == fix
    # The same change isn't redone again.
    queue.next_reconcile = 0
    assert queue.reconcile()
    queue.scan()
    queue.pump()
    assert not harness.runner.active()


async def test_with_eager_redo_off_nothing_starts_but_redo_by_hand_works(
    code_repo: CodeRepo, eager: tuple[Harness, Agents], fake_gh: FakeGh
) -> None:
    harness, agents = eager
    configure(code_repo.project, "automation:\n  eager_redo: false\n")
    feature, (parse, _, rank) = await split_three(code_repo, harness, agents)
    old = {s.node_id: s for s in harness.store.eager_slices(feature)}
    push_on(code_repo, old[parse].branch, "src/fix.py", "fixed = True\n", "Review fix")
    agents.build = rebuild_per_task(agents)
    queue = AutoQueue(harness.runner, debounce_s=0)

    assert queue.reconcile()
    queue.scan()
    queue.pump()
    assert not harness.runner.active()
    build = harness.store.eager_build(feature)
    assert build is not None
    assert build.pending is not None  # shown as stale in the UI
    assert build.round == 1

    await harness.runner.eager.redo(feature)
    await settle(harness)
    build = harness.store.eager_build(feature)
    assert build is not None
    assert (build.state, build.round) == ("split", 2)
    assert git_out(code_repo, "rev-parse", f"origin/{old[rank].branch}") != old[rank].commit
