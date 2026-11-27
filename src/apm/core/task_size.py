"""How big a task's pull request really is: the real lines of code its branch adds, counted the way
the task review counts them (the `apm-task-sizing` skill), and compared with the task size limit.

APM measures it after every implementer turn, and on request. The count is a measure, not a
judgement: a task over its limit is flagged, never refused.
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from apm.core.config import read_config
from apm.core.delivery import branch_ref, branch_tip, default_branch, home_branch
from apm.core.errors import ApmError
from apm.core.git import GitError, git
from apm.core.model import NodeId
from apm.core.project import Project, code_repo, load_graph
from apm.core.settings import feature_settings
from apm.core.split import is_source

_NOT_CODE = re.compile(
    r"""^(\#|//|/\*|\*|--|<!--|"{3}|'{3}"""  # comments and docstring fences
    r"|import\s|from\s+\S+\s+import\s|using\s|use\s|require\(|#include\b"  # imports
    r"|[{}()\[\];,]+$)"  # a line that only closes or opens brackets
)


class SizeRefused(ApmError):
    """The task has nothing to measure yet."""


@dataclass(frozen=True, slots=True)
class TaskSize:
    node_id: NodeId
    branch: str
    sha: str
    """The branch tip that was measured."""
    base: str
    """The commit it was measured from: where it leaves its home branch."""
    lines: int
    limit: int
    files: dict[str, int]
    """Real lines per file, for the files that have any."""
    created: float = field(default_factory=time.time)

    @property
    def over(self) -> bool:
        return self.lines > self.limit


def real_lines(repo_dir: Path, start: str, end: str) -> dict[str, int]:
    """The real lines of code the change from `start` to `end` adds or rewrites, per file: added
    lines of source files (not tests, docs, data or configuration) that aren't blank, a comment, an
    import or only brackets.
    """
    out = git(repo_dir, ["diff", "--no-color", "-U0", start, end])
    counts: dict[str, int] = {}
    path: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            name = line[4:].removeprefix("b/")
            path = name if name != "/dev/null" and is_source(name) else None
        elif path is not None and line.startswith("+"):
            text = line[1:].strip()
            if text and _NOT_CODE.match(text) is None:
                counts[path] = counts.get(path, 0) + 1
    return counts


def measure(project: Project, node_id: NodeId) -> TaskSize:
    """Measures the task's delivery branch against its home branch (fetched as last seen)."""
    graph = load_graph(project)
    delivery = graph[node_id].delivery
    if delivery is None or not delivery.branch:
        raise SizeRefused("this task has no branch yet: Implement it first")
    repo = code_repo(project, SizeRefused)
    tip = branch_tip(repo, delivery.branch)
    if tip is None:
        raise SizeRefused(f"branch {delivery.branch} isn't here or on origin")
    home = home_branch(graph, node_id)
    against = (branch_ref(repo, home) if home else None) or default_branch(repo)
    if against is None:
        raise SizeRefused("there is no home branch to measure the task against")
    try:
        base = git(repo, ["merge-base", against, tip]).strip()
    except GitError:
        raise SizeRefused(f"{delivery.branch} has nothing in common with {against}") from None
    files = real_lines(repo, base, tip)
    limit = feature_settings(read_config(project.apm_dir), graph, node_id).task_max_lines
    return TaskSize(node_id, delivery.branch, tip, base, sum(files.values()), limit, files)


def describe(size: TaskSize) -> str:
    """One line for a run's conversation."""
    files = sorted(size.files.items(), key=lambda kv: -kv[1])[:3]
    where = ", ".join(f"`{path}` {n}" for path, n in files)
    text = f"{size.lines} real lines of code (limit {size.limit})" + (f": {where}" if where else "")
    if size.over:
        return f"**Over the task size limit**: {text}. Consider splitting it before it merges."
    return f"**Size**: {text}."
