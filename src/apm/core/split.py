"""The git side of eager mode (plans.md §7.9): carving a finished eager build into a chain of task
commits, checking a chain before anything is recorded, and moving task branches on.

Everything here is plain git, run by APM itself:

- `mechanical_split`: when the builder made its commits task by task (each with an `APM-Node`
  trailer, in an order the dependencies allow), the split is deterministic: those commits are the
  chain, a task's several commits squashed into one. No agent is needed. Anything else (one big
  commit, commits that mix tasks) goes to the splitter agent.
- `check_chain`: what APM verifies before it believes a split, however it was made: one commit per
  task, each on the one before, each with its task's trailer, none empty, in dependency order, and
  the last one's tree exactly the eager branch's.
- `publish`: pushes task branches without ever rewriting one. A new branch must not exist yet; an
  existing one only moves forward from the commit APM left it at, so a commit someone else pushed
  is never overwritten: git refuses the push.
- `rebuilt_commits`: after a redo, a task's new content goes on top of its branch as a merge
  commit (its old tip, and the new tip of the task before it), so the branch only moves forward
  and its pull request still shows only its own part.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from apm.core.errors import ApmError
from apm.core.git import CommitMessage, GitError, format_commit_message, git, rev_parse
from apm.core.ids import HandleError, resolve_handle
from apm.core.model import NodeId

TRAILER = "APM-Node"
NO_SHA = "0" * 40
DIFF_LIMIT = 20_000
"""How much of a diff goes into an agent's message, in characters."""


class SplitError(ApmError):
    """A split or a branch update APM won't make, and why."""

    http_status = 409


@dataclass(frozen=True, slots=True)
class Piece:
    """One task's commit in a chain."""

    node_id: NodeId
    commit: str
    parent: str
    """The commit it must be built on: the previous piece's, or the chain's base."""


# --- reading commits ---


def tree_of(repo_dir: Path, rev: str) -> str:
    return git(repo_dir, ["rev-parse", f"{rev}^{{tree}}"]).strip()


def parents_of(repo_dir: Path, sha: str) -> list[str]:
    return git(repo_dir, ["rev-list", "--parents", "-n", "1", sha]).split()[1:]


def message_of(repo_dir: Path, sha: str) -> str:
    return git(repo_dir, ["log", "-1", "--format=%B", sha])


def subject_of(repo_dir: Path, sha: str) -> str:
    return git(repo_dir, ["log", "-1", "--format=%s", sha]).strip()


def trailer_values(repo_dir: Path, sha: str) -> list[str]:
    """The values of the commit's `APM-Node` trailers."""
    out = git(repo_dir, ["log", "-1", f"--format=%(trailers:key={TRAILER},valueonly)", sha])
    return [line.strip() for line in out.splitlines() if line.strip()]


def exists(repo_dir: Path, sha: str) -> bool:
    try:
        return rev_parse(repo_dir, sha) is not None
    except GitError:
        return False


def patch_id(repo_dir: Path, start: str, end: str) -> str | None:
    """The stable patch id of the change from `start` to `end`; None if there is none."""
    diff = git(repo_dir, ["diff", "--no-color", start, end])
    if not diff.strip():
        return None
    out = git(repo_dir, ["patch-id", "--stable"], stdin=diff).split()
    return out[0] if out else None


def diff_text(repo_dir: Path, start: str, end: str, limit: int = DIFF_LIMIT) -> str:
    """`git diff start end`, cut to `limit` characters with a line saying how to see the rest."""
    diff = git(repo_dir, ["diff", "--no-color", "--stat", "--patch", start, end])
    if len(diff) <= limit:
        return diff
    return diff[:limit] + f"\n… (cut here: `git diff {start[:12]} {end[:12]}` shows the rest)\n"


_NOT_SOURCE_DIRS = {"test", "tests", "doc", "docs", "__tests__", "spec"}
_NOT_SOURCE_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".lock", ".json", ".yaml", ".yml", ".toml"}
_TEST_NAME = re.compile(r"(^test_|_test\.|\.test\.|\.spec\.)")


def is_source(path: str) -> bool:
    """Roughly what a task's size limit counts: not tests, docs, data or configuration."""
    parts = PurePosixPath(path)
    return (
        not _NOT_SOURCE_DIRS.intersection(parts.parts[:-1])
        and parts.suffix not in _NOT_SOURCE_SUFFIXES
        and _TEST_NAME.search(parts.name) is None
    )


def source_lines(repo_dir: Path, start: str, end: str) -> int:
    """About how many lines of source the change from `start` to `end` adds or rewrites: the
    non-blank added lines of files that aren't tests, docs or configuration. A rough measure, for
    a warning; the task review is what judges a task's size.
    """
    out = git(repo_dir, ["diff", "--no-color", "-U0", start, end])
    count, counting = 0, False
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
            counting = path != "/dev/null" and is_source(path)
        elif counting and line.startswith("+") and line[1:].strip():
            count += 1
    return count


# --- making commits ---


def commit_tree(repo_dir: Path, tree: str, parents: Sequence[str], message: CommitMessage) -> str:
    """A new commit with `tree` and `parents`, made without touching any checkout."""
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    return git(repo_dir, [*args, "-F", "-"], stdin=format_commit_message(message)).strip()


# --- the deterministic split ---


def task_of(value: str, tasks: Collection[NodeId]) -> NodeId | None:
    """The task an `APM-Node` trailer names: its full id, or a handle of exactly one task."""
    try:
        return resolve_handle(value.strip().lower(), tasks)
    except HandleError:
        return None


def mechanical_split(
    repo_dir: Path,
    base: str,
    tip: str,
    tasks: Sequence[NodeId],
    before: Mapping[NodeId, Collection[NodeId]],
) -> list[Piece] | str:
    """The chain, when the builder's commits already are one: every commit from `base` to `tip`
    names exactly one task in its `APM-Node` trailer, a task's commits come one after another,
    every task has some, and the order respects `before` (the tasks each must come after). A
    task's single commit is used as it is; several are squashed into one. Otherwise, why not.
    """
    if git(repo_dir, ["rev-list", "--min-parents=2", f"{base}..{tip}"]).strip():
        return "the build has merge commits"
    commits = git(repo_dir, ["rev-list", "--reverse", f"{base}..{tip}"]).split()
    groups: list[tuple[NodeId, list[str]]] = []
    for sha in commits:
        named = {task for value in trailer_values(repo_dir, sha) if (task := task_of(value, tasks))}
        if len(named) != 1:
            what = "no task" if not named else "several tasks"
            return f"commit {sha[:12]} names {what} in its {TRAILER} trailer"
        (task,) = named
        if groups and groups[-1][0] == task:
            groups[-1][1].append(sha)
        elif any(done == task for done, _ in groups):
            return f"the commits for {task} aren't one after another"
        else:
            groups.append((task, [sha]))
    order = [task for task, _ in groups]
    if missing := [t for t in tasks if t not in order]:
        return f"the build made no commit for {', '.join(missing)}"
    if problem := order_problem(order, before):
        return problem
    pieces: list[Piece] = []
    parent = base
    for task, shas in groups:
        if len(shas) == 1 and parents_of(repo_dir, shas[0]) == [parent]:
            commit = shas[0]
        else:
            commit = commit_tree(
                repo_dir, tree_of(repo_dir, shas[-1]), [parent], squashed(repo_dir, task, shas)
            )
        pieces.append(Piece(task, commit, parent))
        parent = commit
    return pieces


def squashed(repo_dir: Path, task: NodeId, shas: Sequence[str]) -> CommitMessage:
    """The message of a task's commits squashed into one: the first one's subject, every
    subject listed, and the task's trailer.
    """
    subjects = [subject_of(repo_dir, sha) for sha in shas]
    listed = "\n".join(f"- {subject}" for subject in subjects)
    body = f"Squashed from the eager build's commits:\n\n{listed}" if len(shas) > 1 else ""
    return CommitMessage(subject=subjects[0], body=body, trailers=[(TRAILER, task)])


def order_problem(
    order: Sequence[NodeId], before: Mapping[NodeId, Collection[NodeId]]
) -> str | None:
    """Why `order` doesn't respect `before`, or None if it does."""
    seen: set[NodeId] = set()
    for task in order:
        if late := [dep for dep in before.get(task, ()) if dep in order and dep not in seen]:
            return f"{task} comes before {late[0]}, which it depends on"
        seen.add(task)
    return None


# --- checking a chain ---


def check_chain(
    repo_dir: Path,
    base: str,
    tip: str,
    pieces: Sequence[Piece],
    tasks: Sequence[NodeId],
    before: Mapping[NodeId, Collection[NodeId]],
) -> list[str]:
    """What is wrong with a chain, however it was made; empty if nothing is. The chain must hold
    one commit for each of `tasks`, in an order `before` allows, each built on the one before it
    (the first on `base`), each naming its task in an `APM-Node` trailer and changing something,
    and the last one's tree must be `tip`'s: then the chain holds exactly what was built.
    """
    order = [p.node_id for p in pieces]
    problems: list[str] = []
    if unknown := [n for n in order if n not in tasks]:
        problems.append(f"{unknown[0]} isn't one of the tasks being split")
    if len(set(order)) != len(order):
        problems.append("a task has more than one commit in the chain")
    if missing := [t for t in tasks if t not in order]:
        problems.append(f"no commit for {', '.join(missing)}")
    if problem := order_problem(order, before):
        problems.append(problem)
    parent = base
    for piece in pieces:
        problems += _piece_problems(repo_dir, piece, parent)
        parent = piece.commit
    if problems:
        return problems
    if pieces and tree_of(repo_dir, pieces[-1].commit) != tree_of(repo_dir, tip):
        problems.append(
            f"the last task's tree differs from the build's ({tip[:12]}): the split lost or added"
            f" changes; `git diff {pieces[-1].commit[:12]} {tip[:12]}` shows them"
        )
    return problems


def _piece_problems(repo_dir: Path, piece: Piece, parent: str) -> list[str]:
    name = piece.node_id
    if not exists(repo_dir, piece.commit):
        return [f"{name}'s commit {piece.commit[:12]} isn't in the repository"]
    problems = []
    if parents_of(repo_dir, piece.commit) != [parent]:
        problems.append(f"{name}'s commit {piece.commit[:12]} isn't built on {parent[:12]} alone")
    if task_of_trailers(repo_dir, piece.commit, name) is None:
        problems.append(f"{name}'s commit {piece.commit[:12]} has no `{TRAILER}: {name}` trailer")
    if tree_of(repo_dir, piece.commit) == tree_of(repo_dir, parent):
        problems.append(f"{name}'s commit {piece.commit[:12]} changes nothing")
    return problems


def task_of_trailers(repo_dir: Path, sha: str, task: NodeId) -> NodeId | None:
    values = trailer_values(repo_dir, sha)
    return task if any(task_of(v, [task]) == task for v in values) else None


# --- moving branches, never backwards ---


@dataclass(frozen=True, slots=True)
class Move:
    branch: str
    new: str
    old: str | None
    """Where APM left the branch, which it must still be at; None for a branch that must not
    exist yet."""


def remote_tip(repo_dir: Path, branch: str) -> str | None:
    """`branch`'s tip on `origin` as last fetched, else locally if there is no `origin`."""
    ref = f"refs/remotes/origin/{branch}" if has_origin(repo_dir) else f"refs/heads/{branch}"
    try:
        return rev_parse(repo_dir, ref)
    except GitError:
        return None


def has_origin(repo_dir: Path) -> bool:
    try:
        git(repo_dir, ["remote", "get-url", "origin"])
    except GitError:
        return False
    return True


def publish(repo_dir: Path, moves: Sequence[Move]) -> None:
    """Moves every branch, all or none. Each must be where APM expects: a new one must not exist
    (unless it already holds the commit), an existing one must still be at `old`. The pushes are
    plain ones, never forced, so even a commit pushed after the check stops them. Without an
    `origin` the local branches move, under the same rule. Raises `SplitError` if anything is
    in the way.
    """
    todo = []
    for move in moves:
        now = remote_tip(repo_dir, move.branch)
        if now == move.new:
            continue
        if now != move.old:
            raise SplitError(_in_the_way(move, now))
        todo.append(move)
    if not todo:
        return
    if not has_origin(repo_dir):
        for move in todo:
            git(repo_dir, ["update-ref", f"refs/heads/{move.branch}", move.new, move.old or NO_SHA])
        return
    refspecs = [f"{m.new}:refs/heads/{m.branch}" for m in todo]
    try:
        git(repo_dir, ["push", "--quiet", "--atomic", "origin", *refspecs])
    except GitError as err:
        raise SplitError(f"git refused to move the task branches: {err.stderr.strip()}") from None
    fetched = [f"+refs/heads/{m.branch}:refs/remotes/origin/{m.branch}" for m in todo]
    git(repo_dir, ["fetch", "--quiet", "origin", *fetched])


def _in_the_way(move: Move, now: str | None) -> str:
    if move.old is None:
        return (
            f"branch {move.branch} already exists (at {now[:12] if now else '?'}): APM won't"
            " overwrite it. Delete it, or rename the task, and try again"
        )
    if now is None:
        return f"branch {move.branch} is gone: APM won't make it again on its own"
    return (
        f"branch {move.branch} has commits APM didn't make (it is at {now[:12]}, APM left it at"
        f" {move.old[:12]}): APM won't overwrite them"
    )


def rebuilt_commits(
    repo_dir: Path, pieces: Sequence[Piece], old_tips: Mapping[NodeId, str], base: str, why: str
) -> list[str]:
    """For each rebuilt task, the commit that brings its branch to the new content without
    rewriting it: a merge of its old tip and the task before it as rebuilt (`base` for the
    first), whose tree is the new piece's. The pull request's diff against the task before it
    is then exactly the task's new part.
    """
    out: list[str] = []
    below = base
    for piece in pieces:
        subject = subject_of(repo_dir, piece.commit)
        message = CommitMessage(
            subject=f"Rebuild: {subject}"[:200],
            body=(
                f"APM rebuilt this task on top of a change before it ({why}). The commits before"
                " this one are the task as first built; this one holds it as it is now."
            ),
            trailers=[(TRAILER, piece.node_id)],
        )
        tree = tree_of(repo_dir, piece.commit)
        below = commit_tree(repo_dir, tree, [old_tips[piece.node_id], below], message)
        out.append(below)
    return out
