"""The per-task review lock: `.cache/task-review/<id>.json`, held while a task's review runs.

While it is held, the plan refuses to change the task (see `ops`), except its status and delivery.
The marker records the holder's pid and host; one whose pid is dead on this host doesn't count.
"""

import json
import os
import socket
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from apm.core.errors import ApmError
from apm.core.ids import handle
from apm.core.lock import CACHE_DIR, pid_alive
from apm.core.model import NodeId

REVIEW_DIR = "task-review"


class TaskUnderReview(ApmError):  # noqa: N818 (the name the plan uses)
    http_status = 409

    def __init__(self, node_id: NodeId) -> None:
        super().__init__(f"{handle(node_id)} is under review: wait for its review to finish")
        self.node_id = node_id


def review_lock_path(apm_dir: Path, node_id: NodeId) -> Path:
    return apm_dir / CACHE_DIR / REVIEW_DIR / f"{node_id}.json"


def take_review_lock(apm_dir: Path, node_id: NodeId, run: str, pid: int | None = None) -> None:
    path = review_lock_path(apm_dir, node_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = {"pid": pid or os.getpid(), "hostname": socket.gethostname(), "run": run}
    path.write_text(json.dumps(owner), encoding="utf-8")


def read_review_lock(apm_dir: Path, node_id: NodeId) -> dict[str, Any] | None:
    try:
        return json.loads(review_lock_path(apm_dir, node_id).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None


def release_review_lock(apm_dir: Path, node_id: NodeId) -> None:
    review_lock_path(apm_dir, node_id).unlink(missing_ok=True)


def under_review(apm_dir: Path, node_id: NodeId) -> bool:
    owner = read_review_lock(apm_dir, node_id)
    if not isinstance(owner, dict):
        return False
    # A pid on another host can't be checked, so its marker always counts.
    return owner.get("hostname") != socket.gethostname() or pid_alive(int(owner.get("pid", 0)))


def refuse_under_review(apm_dir: Path, node_ids: Iterable[NodeId | None]) -> None:
    for node_id in node_ids:
        if node_id is not None and under_review(apm_dir, node_id):
            raise TaskUnderReview(node_id)
