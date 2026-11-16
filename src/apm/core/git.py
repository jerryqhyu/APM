"""A thin wrapper over the git CLI (plans.md §3.5).

Commits go through `git` itself, so the user's identity, signing and hooks all apply. Commands run
from an argument list, never a shell. Output is decoded as UTF-8 with line endings untouched, so
`show` returns a file exactly as committed.
"""

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
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
