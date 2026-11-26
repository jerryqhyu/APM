"""The plain code of eager mode (plans.md §7.9): what a build covers, the gate, the split checks,
moving branches safely, and redo detection, against real git repos.
"""

from pathlib import Path

import pytest

from apm.core.config import read_config
from apm.core.delivery import PrRef, PullRequest, branch_tip, ensure_feature_branch
from apm.core.eager import (
    Change,
    build_blockers,
    build_tasks,
    eager_branch,
    eager_feature,
    find_changes,
    record_split,
    redirect,
    task_branch,
    task_refusal,
)
from apm.core.files import read_body
from apm.core.git import CommitMessage, git
from apm.core.model import NodeId, Status
from apm.core.ops import NewNode, create_nodes, decompose, set_status
from apm.core.project import Project, load_graph
from apm.core.split import (
    Move,
    Piece,
    SplitError,
    check_chain,
    commit_tree,
    is_source,
    mechanical_split,
    publish,
    source_lines,
    tree_of,
)
from apm.core.store import EagerBuild, EagerSlice, Store, TaskReview, content_fingerprint
from apm.runner.agents import slug, worktree_name
from tests.conftest import CodeRepo

EAGER_ON = "features:\n  eager: true\n"


def configure(project: Project, text: str) -> None:
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + text)


def out(repo: Path, *args: str) -> str:
    return git(repo, list(args)).strip()


def feature_of_three(project: Project) -> tuple[NodeId, list[NodeId]]:
    """A feature whose tasks are Rank → Index → Parse, created in the opposite order."""
    feature = create_nodes(project, [NewNode(title="Search")]).created[0]
    tasks = decompose(
        project,
        feature,
        [
            NewNode(title="Rank", key="r", depends_on_keys=["i"]),
            NewNode(title="Index", key="i", depends_on_keys=["p"]),
            NewNode(title="Parse", key="p"),
        ],
    ).created
    return feature, list(tasks)


def commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    git(repo, ["add", name])
    git(repo, ["commit", "--quiet", "-m", message])
    return out(repo, "rev-parse", "HEAD")


# --- what a build covers, and the gate ---


def test_a_build_covers_the_features_own_to_do_tasks_in_dependency_order(
    project: Project,
) -> None:
    feature, (rank, index, parse) = feature_of_three(project)
    (nested,) = decompose(project, feature, [NewNode(title="Nested")]).created
    decompose(project, nested, [NewNode(title="Inside")])
    (done,) = decompose(project, feature, [NewNode(title="Done")]).created
    set_status(project, done, Status.DONE)
    graph = load_graph(project)
    assert build_tasks(graph, feature) == [parse, index, rank]  # not nested, done or the step

    config = read_config(project.apm_dir)
    assert eager_feature(config, graph, feature) is None  # eager is off by default
    configure(project, EAGER_ON)
    config = read_config(project.apm_dir)
    assert eager_feature(config, graph, feature) == feature
    assert eager_feature(config, graph, parse) == feature
    step = graph.review_step(feature)
    assert step is not None
    assert eager_feature(config, graph, step) is None


def test_the_gate_names_every_task_that_holds_a_build_back(project: Project) -> None:
    feature, (rank, index, parse) = feature_of_three(project)
    (outside,) = create_nodes(project, [NewNode(title="Elsewhere")]).created
    (late,) = decompose(project, feature, [NewNode(title="Late", depends_on=[outside])]).created
    store = Store(project.apm_dir)
    graph = load_graph(project)
    for task in (rank, index, late):
        body = read_body(project.apm_dir, task)
        fingerprint = content_fingerprint(graph[task], body)
        store.add_task_review(TaskReview(task, fingerprint, "pass", {}))
    tasks = build_tasks(graph, feature)
    blockers = dict(
        build_blockers(
            graph,
            store,
            read_config(project.apm_dir),
            tasks,
            lambda n: read_body(project.apm_dir, n),
            lambda n: n == index,
        )
    )
    assert set(blockers) == {parse, index, late}
    assert "has not been reviewed" in blockers[parse]
    assert "under review" in blockers[index]
    assert "isn't part of this build and isn't done" in blockers[late]


def test_branch_names_follow_the_implementers(project: Project) -> None:
    feature, (rank, _, _) = feature_of_three(project)
    graph = load_graph(project)
    assert task_branch(graph, rank) == "apm/" + worktree_name(graph, rank).removeprefix("apm-")
    assert slug("Ünïcode & More!") == "n-code-more"
    assert eager_branch(graph, feature, 1) == f"eager/{feature[-8:]}-search"
    assert eager_branch(graph, feature, 3) == f"eager/{feature[-8:]}-search-3"


# --- the mechanical split and the chain checks ---


@pytest.fixture
def built(code_repo: CodeRepo) -> tuple[Path, str, list[NodeId]]:
    """A base commit on main, and three task ids (none of them in a plan)."""
    tasks = [
        "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a01",
        "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a02",
        "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a03",
    ]
    return code_repo.dir, out(code_repo.dir, "rev-parse", "HEAD"), tasks


def test_commits_made_task_by_task_are_the_chain(built: tuple[Path, str, list[NodeId]]) -> None:
    repo, base, (a, b, c) = built
    first = commit(repo, "a.py", "a = 1\n", f"A\n\nAPM-Node: {a}")
    commit(repo, "b.py", "b = 1\n", f"B\n\nAPM-Node: {b[-8:]}")  # a handle is enough
    commit(repo, "b2.py", "b = 2\n", f"B again\n\nAPM-Node: {b}")
    tip = commit(repo, "c.py", "c = 1\n", f"C\n\nAPM-Node: {c}")
    before = {a: frozenset(), b: frozenset({a}), c: frozenset({a, b})}

    pieces = mechanical_split(repo, base, tip, [a, b, c], before)
    assert isinstance(pieces, list)
    assert [p.node_id for p in pieces] == [a, b, c]
    assert pieces[0].commit == first  # used as it is
    assert out(repo, "rev-parse", f"{pieces[1].commit}^") == first  # B's two, squashed
    assert f"APM-Node: {b}" in out(repo, "log", "-1", "--format=%B", pieces[1].commit)
    assert pieces[2].commit != tip  # re-parented onto the squash, same tree
    assert tree_of(repo, pieces[2].commit) == tree_of(repo, tip)
    assert check_chain(repo, base, tip, pieces, [a, b, c], before) == []


@pytest.mark.parametrize(
    ("commits", "why"),
    [
        ([("a.py", "a"), ("b.py", None), ("c.py", "c")], "names no task"),
        ([("a.py", "a"), ("b.py", "b"), ("a2.py", "a"), ("c.py", "c")], "aren't one after another"),
        ([("a.py", "a"), ("c.py", "c")], "made no commit for"),
        ([("b.py", "b"), ("a.py", "a"), ("c.py", "c")], "which it depends on"),
    ],
)
def test_a_build_that_isnt_task_by_task_needs_the_splitter(
    built: tuple[Path, str, list[NodeId]], commits: list[tuple[str, str | None]], why: str
) -> None:
    repo, base, (a, b, c) = built
    ids = {"a": a, "b": b, "c": c}
    tip = base
    for name, task in commits:
        trailer = f"\n\nAPM-Node: {ids[task]}" if task else ""
        tip = commit(repo, name, name, f"{name}{trailer}")
    before = {a: frozenset(), b: frozenset({a}), c: frozenset({b})}
    reason = mechanical_split(repo, base, tip, [a, b, c], before)
    assert isinstance(reason, str)
    assert why in reason


def test_a_chain_is_checked_before_anything_is_believed(
    built: tuple[Path, str, list[NodeId]],
) -> None:
    repo, base, (a, b, _) = built
    tip = commit(repo, "a.py", "a\n", "build")
    tip = commit(repo, "b.py", "b\n", "build")
    tree_a = tree_of(repo, base)
    before = {a: frozenset(), b: frozenset({a})}

    def make(tree: str, parent: str, task: str | None) -> str:
        trailers = [("APM-Node", task)] if task else []
        return commit_tree(repo, tree, [parent], CommitMessage(subject="x", trailers=trailers))

    good_a = make(out(repo, "rev-parse", f"{tip}^:"), base, a)
    good_b = make(tree_of(repo, tip), good_a, b)
    assert (
        check_chain(
            repo, base, tip, [Piece(a, good_a, base), Piece(b, good_b, good_a)], [a, b], before
        )
        == []
    )

    unsigned = make(tree_of(repo, tip), good_a, None)
    problems = check_chain(
        repo, base, tip, [Piece(a, good_a, base), Piece(b, unsigned, good_a)], [a, b], before
    )
    assert any("trailer" in p for p in problems)
    empty = make(tree_a, base, a)
    problems = check_chain(repo, base, tip, [Piece(a, empty, base)], [a, b], before)
    assert any("changes nothing" in p for p in problems)
    assert any(f"no commit for {b}" in p for p in problems)
    short = make(out(repo, "rev-parse", f"{tip}^:"), good_a, b)  # B's part is missing
    problems = check_chain(
        repo, base, tip, [Piece(a, good_a, base), Piece(b, short, good_a)], [a, b], before
    )
    assert any("changes nothing" in p for p in problems)
    off_base = make(tree_of(repo, tip), tip, a)
    problems = check_chain(repo, base, tip, [Piece(a, off_base, base)], [a], {a: frozenset()})
    assert any("isn't built on" in p for p in problems)
    lost = make(out(repo, "rev-parse", f"{tip}^:"), base, a)
    problems = check_chain(repo, base, tip, [Piece(a, lost, base)], [a], {a: frozenset()})
    assert any("differs from the build's" in p for p in problems)


def test_the_size_estimate_counts_source_lines_only(built: tuple[Path, str, list[NodeId]]) -> None:
    repo, base, _ = built
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("a = 1\n\nb = 2\n")
    (repo / "tests" / "test_app.py").write_text("x = 1\n" * 20)
    (repo / "README.md").write_text("words\n" * 20)
    git(repo, ["add", "src", "tests", "README.md"])
    git(repo, ["commit", "--quiet", "-m", "work"])
    assert source_lines(repo, base, "HEAD") == 2
    assert is_source("src/apm/core/eager.py")
    assert not is_source("ui/src/App.test.tsx")
    assert not is_source("docs/user-guide.md")
    assert not is_source(".gitignore")


# --- moving branches without rewriting them ---


def test_branches_only_move_from_where_apm_left_them(code_repo: CodeRepo) -> None:
    repo = code_repo.dir
    base = out(repo, "rev-parse", "HEAD")
    one = commit(repo, "one.py", "1\n", "one")
    two = commit(repo, "two.py", "2\n", "two")
    publish(repo, [Move("apm/x", one, None)])
    assert branch_tip(repo, "apm/x") == one
    publish(repo, [Move("apm/x", one, None)])  # already there: nothing to do
    with pytest.raises(SplitError, match="already exists"):
        publish(repo, [Move("apm/x", two, None)])
    with pytest.raises(SplitError, match="commits APM didn't make"):
        publish(repo, [Move("apm/x", two, base)])
    publish(repo, [Move("apm/x", two, one)])
    assert branch_tip(repo, "apm/x") == two
    # All or none: one refusal stops every move.
    with pytest.raises(SplitError):
        publish(repo, [Move("apm/y", one, None), Move("apm/x", one, base)])
    assert branch_tip(repo, "apm/y") is None


# --- redo detection ---


@pytest.fixture
def chain(code_repo: CodeRepo) -> tuple[CodeRepo, Store, NodeId, list[EagerSlice], str]:
    """A feature split into a chain of three on its feature branch, recorded."""
    project = code_repo.project
    configure(project, EAGER_ON)
    feature, (rank, index, parse) = feature_of_three(project)
    repo = code_repo.dir
    home = ensure_feature_branch(project, repo, feature)
    base = out(repo, "rev-parse", f"origin/{home}")
    git(repo, ["checkout", "--quiet", "-B", "work", base])
    pieces, parent = [], base
    for task in (parse, index, rank):
        sha = commit(repo, f"{task[-6:]}.py", f"{task}\n", f"task\n\nAPM-Node: {task}")
        pieces.append(Piece(task, sha, parent))
        parent = sha
    git(repo, ["checkout", "--quiet", "main"])
    store = Store(project.apm_dir)
    build = EagerBuild(feature, 1, "eager/x", home, base, None, "splitting", (parse, index, rank))
    store.save_eager_build(build)
    record_split(project, store, build, parent, pieces, run="r")
    return code_repo, store, feature, store.eager_slices(feature), home


def pr(base: str, *, merged: bool = False, merge: str | None = None) -> PullRequest:
    ref = PrRef("github.com", "o", "r", 1)
    return PullRequest(ref, "closed" if merged else "open", merged, base, merge)


def move_branch(repo: Path, branch: str, sha: str) -> None:
    """Someone else moves a branch on `origin` (a push, or a merge)."""
    git(repo, ["push", "--quiet", "--force", "origin", f"{sha}:refs/heads/{branch}"])
    git(repo, ["fetch", "--quiet", "origin"])


def test_nothing_changed_is_no_change(
    chain: tuple[CodeRepo, Store, NodeId, list[EagerSlice], str],
) -> None:
    repo, _, _, slices, home = chain
    assert find_changes(repo.dir, slices, {}, home).changes == ()


def test_new_commits_on_a_task_branch_are_a_change(
    chain: tuple[CodeRepo, Store, NodeId, list[EagerSlice], str],
) -> None:
    repo, _, _, slices, home = chain
    first = slices[0]
    git(repo.dir, ["checkout", "--quiet", "-B", "fix", first.commit])
    fix = commit(repo.dir, "fix.py", "fix\n", "Review fix")
    git(repo.dir, ["checkout", "--quiet", "main"])
    move_branch(repo.dir, first.branch, fix)
    found = find_changes(repo.dir, slices, {}, home)
    assert found.changes == (Change(first.node_id, "pushed", fix),)

    # A new commit that changes nothing, or a later task merged into this branch, is not one.
    git(repo.dir, ["checkout", "--quiet", "-B", "fix", first.commit])
    git(repo.dir, ["commit", "--quiet", "--allow-empty", "-m", "nothing"])
    empty = out(repo.dir, "rev-parse", "HEAD")
    git(repo.dir, ["checkout", "--quiet", "main"])
    move_branch(repo.dir, first.branch, empty)
    assert find_changes(repo.dir, slices, {}, home).changes == ()
    move_branch(repo.dir, first.branch, slices[1].commit)
    assert find_changes(repo.dir, slices, {}, home).changes == ()


def test_a_merge_as_split_lands_and_a_merge_with_changes_is_a_change(
    chain: tuple[CodeRepo, Store, NodeId, list[EagerSlice], str],
) -> None:
    repo, _, _, slices, home = chain
    first, second = slices[0], slices[1]
    base = out(repo.dir, "rev-parse", f"origin/{home}")
    # A merge commit of the task's own commit: as split.
    git(repo.dir, ["checkout", "--quiet", "-B", "merging", base])
    git(repo.dir, ["merge", "--quiet", "--no-ff", "-m", "Merge #1", first.commit])
    merged = out(repo.dir, "rev-parse", "HEAD")
    # Then the next one squashed on top of other work: still as split (same patch).
    commit(repo.dir, "other.py", "other\n", "Someone else's")
    git(repo.dir, ["checkout", "--quiet", second.commit, "--", f"{second.node_id[-6:]}.py"])
    git(repo.dir, ["commit", "--quiet", "-m", "Index (#2)"])
    squashed = out(repo.dir, "rev-parse", "HEAD")
    # And one squashed with an edit: a change.
    third = slices[2]
    git(repo.dir, ["checkout", "--quiet", third.commit, "--", f"{third.node_id[-6:]}.py"])
    (repo.dir / "edit.py").write_text("edited\n")
    git(repo.dir, ["add", "-A"])
    git(repo.dir, ["commit", "--quiet", "-m", "Rank (#3)"])
    edited = out(repo.dir, "rev-parse", "HEAD")
    git(repo.dir, ["checkout", "--quiet", "main"])
    prs = {
        first.node_id: pr(home, merged=True, merge=merged),
        second.node_id: pr(home, merged=True, merge=squashed),
        third.node_id: pr(home, merged=True, merge=edited),
    }
    found = find_changes(repo.dir, slices, prs, home)
    assert found.landed == ((first.node_id, merged), (second.node_id, squashed))
    assert found.changes == (Change(third.node_id, "merged", edited),)
    # Merged into the task before it, or by a commit this clone hasn't seen: nothing to say yet.
    prs[third.node_id] = pr(second.branch, merged=True, merge=edited)
    assert find_changes(repo.dir, slices, prs, home).changes == ()
    prs[third.node_id] = pr(home, merged=True, merge="f" * 40)
    assert find_changes(repo.dir, slices, prs, home).changes == ()


def test_single_task_implement_follows_the_build_state(
    chain: tuple[CodeRepo, Store, NodeId, list[EagerSlice], str],
) -> None:
    repo, store, feature, slices, _ = chain
    project = repo.project
    graph, config = load_graph(project), read_config(project.apm_dir)
    assert "use Redo" in (task_refusal(store, config, graph, slices[0].node_id) or "")
    assert redirect(store, config, graph, slices[0].node_id) is None
    (later,) = decompose(project, feature, [NewNode(title="Later")]).created
    graph = load_graph(project)
    assert task_refusal(store, config, graph, later) is None
    assert redirect(store, config, graph, later) is None  # not part of the split: on its own
