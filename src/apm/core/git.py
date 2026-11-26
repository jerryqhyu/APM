"""A thin wrapper over the git CLI (plans.md §3.5).

Commits go through `git` itself, so the user's identity, signing and hooks all apply. Commands run
from an argument list, never a shell. Output is decoded as UTF-8 with line endings untouched, so
`show` returns a file exactly as committed.
"""

import contextlib
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from apm.core.errors import ApmError


class GitError(ApmError, RuntimeError):
    http_status = 500

    def __init__(self, command: Sequence[str], exit_code: int | None, stderr: str) -> None:
        code = "" if exit_code is None else f" ({exit_code})"
        super().__init__(f"git {' '.join(command)} failed{code}: {stderr.strip()}")
        self.command = tuple(command)
        self.exit_code = exit_code
        """None if git couldn't be started."""
        self.stderr = stderr


def git(cwd: Path, args: Sequence[str], stdin: str = "") -> str:
    """Runs `git -C <cwd> <args>` and returns stdout. Raises `GitError` on a non-zero exit."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], input=stdin.encode(), capture_output=True, check=False
        )
    except OSError as err:
        raise GitError(args, None, str(err)) from err
    if result.returncode != 0:
        raise GitError(args, result.returncode, result.stderr.decode(errors="replace"))
    return result.stdout.decode()


def add(cwd: Path, paths: Sequence[str]) -> None:
    if paths:
        git(cwd, ["add", "--", *paths])


_TRAILER_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")

type Trailers = Sequence[tuple[str, str]]
"""Trailers in the order they're written, e.g. `[("APM-Op", "create")]`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitMessage:
    subject: str
    body: str = ""
    trailers: Trailers = ()


def format_commit_message(message: CommitMessage) -> str:
    """Subject, optional body, then trailers as the last paragraph, where `git log` finds them."""
    subject = message.subject
    if not subject.strip() or "\r" in subject or "\n" in subject:
        raise ValueError("commit subject must be one non-blank line")
    for key, value in message.trailers:
        if _TRAILER_KEY_RE.fullmatch(key) is None:
            raise ValueError(f"invalid trailer key: {key!r}")
        if not value.strip() or "\r" in value or "\n" in value:
            raise ValueError(f"trailer {key} must be one non-blank line")
    parts = [subject]
    if message.body.strip():
        parts.append(message.body.strip())
    if message.trailers:
        parts.append("\n".join(f"{key}: {value}" for key, value in message.trailers))
    return "\n\n".join(parts) + "\n"


def commit(cwd: Path, message: CommitMessage) -> str:
    """Commits what's staged and returns the new commit's SHA."""
    # --cleanup=whitespace keeps lines starting with "#", which the default would strip.
    git(
        cwd,
        ["commit", "--quiet", "--cleanup=whitespace", "-F", "-"],
        format_commit_message(message),
    )
    sha = rev_parse(cwd, "HEAD")
    if sha is None:
        raise GitError(["rev-parse", "HEAD"], None, "no HEAD after committing")
    return sha


def rev_parse(cwd: Path, rev: str) -> str | None:
    """The full SHA of a commit, or None if `rev` doesn't name one (e.g. HEAD in an empty repo)."""
    try:
        out = git(
            cwd, ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"]
        )
    except GitError as err:
        if err.exit_code == 1:
            return None
        raise
    return out.strip()


def show(cwd: Path, rev: str, path: str) -> str:
    """A file's contents at a commit. Raises `GitError` if the file or commit doesn't exist."""
    return git(cwd, ["show", f"{rev}:{path}"])


def is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    """True if `ancestor` is `descendant` or one of its ancestors."""
    try:
        git(cwd, ["merge-base", "--is-ancestor", ancestor, descendant])
    except GitError as err:
        if err.exit_code == 1:
            return False
        raise
    return True


def ref_tip(cwd: Path, ref: str) -> str | None:
    """The commit `ref` points at, or None if it doesn't exist (or git can't tell)."""
    try:
        return rev_parse(cwd, ref)
    except GitError:
        return None


def tree_of(cwd: Path, rev: str) -> str:
    return git(cwd, ["rev-parse", f"{rev}^{{tree}}"]).strip()


def parents_of(cwd: Path, sha: str) -> list[str]:
    return git(cwd, ["rev-list", "--parents", "-n", "1", sha]).split()[1:]


def patch_id(
    cwd: Path, start: str, end: str | None = None, *, diff: str | None = None
) -> str | None:
    """The stable patch id of the change from `start` to `end` (or of `diff`); None if empty."""
    if diff is None:
        diff = git(cwd, ["diff", "--no-color", start, end or "HEAD"])
    if not diff.strip():
        return None
    out = git(cwd, ["patch-id", "--stable"], stdin=diff).split()
    return out[0] if out else None


def has_origin(cwd: Path) -> bool:
    try:
        git(cwd, ["remote", "get-url", "origin"])
    except GitError:
        return False
    return True


def checkout_detached(repo: Path, name: str, sha: str) -> Path:
    """A fresh detached checkout of `sha` at `.claude/worktrees/<name>`, replacing any there."""
    path = repo / ".claude" / "worktrees" / name
    if path.exists():
        with contextlib.suppress(GitError):
            git(repo, ["worktree", "remove", "--force", "--force", str(path)])
        shutil.rmtree(path, ignore_errors=True)
    with contextlib.suppress(GitError):
        git(repo, ["worktree", "prune"])
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, ["worktree", "add", "--quiet", "--detach", str(path), sha])
    return path


NO_SHA = "0" * 40
"""What `update-ref` takes as the old value of a branch that must not exist yet."""


def publish(repo: Path, moves: Mapping[str, tuple[str, str | None]]) -> None:
    """Moves branches (name → (new, old)): one plain, atomic push to `origin` (never forced), then
    their tracking refs are refreshed. Without an `origin`, the local branches move instead, each
    only if it is still at `old` (None: it must not exist yet).
    """
    if not moves:
        return
    if not has_origin(repo):
        for branch, (new, old) in moves.items():
            git(repo, ["update-ref", f"refs/heads/{branch}", new, old or NO_SHA])
        return
    git(
        repo,
        [
            "push",
            "--quiet",
            "--atomic",
            "origin",
            *(f"{new}:refs/heads/{b}" for b, (new, _) in moves.items()),
        ],
    )
    git(
        repo,
        [
            "fetch",
            "--quiet",
            "origin",
            *(f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in moves),
        ],
    )


@dataclass(frozen=True, slots=True)
class LogEntry:
    sha: str
    time: int
    """Committer time, Unix seconds."""
    subject: str
    trailers: Mapping[str, str] = field(default_factory=dict)
    """Trailers by key; if a key repeats, the first value wins."""


def log(
    cwd: Path, revisions: str = "HEAD", paths: Sequence[str] = (), *, reverse: bool = False
) -> list[LogEntry]:
    """Commits with their trailers, newest first unless `reverse`. `revisions` is a revision or a
    range such as `<sha>..HEAD`. An empty repo has no log: returns [].
    """
    if revisions == "HEAD" and rev_parse(cwd, "HEAD") is None:
        return []
    args = ["log", "--format=%H%x00%ct%x00%s%x00%(trailers:only,unfold)%x1e"]
    if reverse:
        args.append("--reverse")
    args += ["--end-of-options", revisions, "--", *paths]
    entries = []
    for record in git(cwd, args).split("\x1e"):
        record = record.removeprefix("\n")
        if record:
            sha, time, subject, trailer_text = record.split("\0")
            entries.append(LogEntry(sha, int(time), subject, _parse_trailers(trailer_text)))
    return entries


def _parse_trailers(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, colon, value = line.partition(":")
        if colon and key.strip():
            out.setdefault(key.strip(), value.strip())
    return out
