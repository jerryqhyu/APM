"""Where a task's pull request stands on GitHub right now: open, draft, merged or closed, what its
reviewers said, and whether its checks pass. Read with `gh api` (REST), and kept for a while, so the
UI and `apm serve`'s loop can both ask often without asking GitHub each time.
"""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm.core.delivery import GhError, gh, parse_pr_url
from apm.core.graph import Graph
from apm.core.model import NodeId, Status

MAX_AGE_S = 60.0
"""How long a status read from GitHub is good for."""

_FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}


@dataclass(frozen=True, slots=True)
class PrStatus:
    url: str
    state: str
    """`open`, `draft`, `merged` or `closed` (without merging)."""
    review: str
    """`approved`, `changes_requested`, or `waiting` for a review."""
    checks: str
    """`passing`, `failing`, `running`, or `none` when the pull request has no checks."""
    checked: float
    """When GitHub was asked, Unix seconds."""


def read_pr_status(repo_dir: Path, url: str, now: float) -> PrStatus | None:
    """Asks GitHub. None if it can't be read: not a GitHub pull request URL, no `gh`, not signed
    in, offline.
    """
    ref = parse_pr_url(url)
    if ref is None:
        return None
    try:
        pr = _get(repo_dir, ref.host_args, ref.api_path)
        if pr.get("merged") or pr.get("merged_at"):
            return PrStatus(url, "merged", "waiting", "none", now)
        if pr["state"] == "closed":
            return PrStatus(url, "closed", "waiting", "none", now)
        reviews = _get(repo_dir, ref.host_args, f"{ref.api_path}/reviews")
        checks = f"repos/{ref.owner}/{ref.repo}/commits/{pr['head']['sha']}/check-runs"
        runs = _get(repo_dir, ref.host_args, checks)
    except GhError, ValueError, KeyError, TypeError:
        return None
    state = "draft" if pr.get("draft") else "open"
    return PrStatus(url, state, _review(reviews), _checks(runs.get("check_runs", [])), now)


def _get(repo_dir: Path, host_args: list[str], path: str) -> Any:
    return json.loads(gh(repo_dir, ["api", *host_args, path]))


def _review(reviews: list[dict[str, Any]]) -> str:
    """Each reviewer's latest say counts; a comment says nothing either way."""
    latest: dict[str, str] = {}
    for review in reviews:
        state = str(review.get("state", "")).upper()
        who = str((review.get("user") or {}).get("login", ""))
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[who] = state
    said = set(latest.values())
    if "CHANGES_REQUESTED" in said:
        return "changes_requested"
    return "approved" if "APPROVED" in said else "waiting"


def _checks(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "none"
    if any(str(r.get("conclusion") or "") in _FAILED for r in runs):
        return "failing"
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    return "passing"


class PrStatuses:
    """The latest status of each pull request asked about, shared by the server and its loop."""

    def __init__(self, repo_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.repo_dir = repo_dir
        self.clock = clock
        self._known: dict[str, PrStatus] = {}
        self._lock = threading.Lock()

    def get(self, url: str, max_age: float = MAX_AGE_S) -> PrStatus | None:
        """The status of `url`, from GitHub if what is known is older than `max_age`. When GitHub
        can't be asked, the last known status (or None).
        """
        with self._lock:
            known = self._known.get(url)
        if known is not None and self.clock() - known.checked < max_age:
            return known
        fresh = read_pr_status(self.repo_dir, url, self.clock())
        if fresh is None:
            return known
        with self._lock:
            self._known[url] = fresh
        return fresh

    def known(self, url: str) -> PrStatus | None:
        """What is known about `url`, without asking GitHub."""
        with self._lock:
            return self._known.get(url)


def awaiting_merge(graph: Graph) -> dict[NodeId, str]:
    """The tasks whose work is delivered with a pull request and not yet merged: in progress, with a
    pull request on record. Node → pull request URL.
    """
    return {
        node_id: node.delivery.pr
        for node_id, node in graph.nodes.items()
        if node.status is Status.IN_PROGRESS and node.delivery is not None and node.delivery.pr
    }
