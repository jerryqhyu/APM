"""`.cache/write.lock` (plans.md §6.2): one writer at a time, across processes.

The lock file is created with O_EXCL and records its owner. It's stale if its owner's PID is dead on
this host. Breaking a stale lock happens under a second O_EXCL file, `write.lock.break`, so two
processes can't both judge the same lock stale and one delete the other's new lock.
"""

import json
import os
import secrets
import socket
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

CACHE_DIR = ".cache"
LOCK_FILE = "write.lock"

# A lock file that can't be parsed is normally mid-write; after this long, it's abandoned.
_UNREADABLE_GRACE_S = 2.0
# A break file left by a process that died mid-break.
_BREAK_GRACE_S = 5.0


def lock_path(apm_dir: Path) -> Path:
    return apm_dir / CACHE_DIR / LOCK_FILE


@dataclass(frozen=True, slots=True)
class LockOwner:
    pid: int
    hostname: str
    acquired: str
    """ISO timestamp."""
    token: str
    """Random, so an owner can tell its lock from a later one."""


class LockTimeoutError(TimeoutError):
    def __init__(self, path: Path, timeout: float, owner: LockOwner | None) -> None:
        by = f" by pid {owner.pid} on {owner.hostname} since {owner.acquired}" if owner else ""
        super().__init__(f"timed out after {timeout}s waiting for {path}, held{by}")
        self.owner = owner
        """Who held the lock when we gave up, if the file was readable."""


class WriteLock:
    """A held write lock; also a context manager that releases it on exit."""

    def __init__(self, path: Path, owner: LockOwner) -> None:
        self.path = path
        self.owner = owner
        self._released = False

    def release(self) -> None:
        """Removes the lock if it's still ours. Safe to call twice."""
        if self._released:
            return
        self._released = True
        current = _read_owner(self.path)
        if current is not None and current.token == self.owner.token:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> WriteLock:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, err: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


def acquire_write_lock(apm_dir: Path, *, timeout: float = 10.0, retry: float = 0.025) -> WriteLock:
    """Waits up to `timeout` seconds for the lock, then raises `LockTimeoutError`."""
    path = lock_path(apm_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = LockOwner(
        pid=os.getpid(),
        hostname=socket.gethostname(),
        acquired=datetime.now(UTC).isoformat(),
        token=secrets.token_hex(8),
    )
    deadline = time.monotonic() + timeout
    while True:
        if _create_exclusive(path, json.dumps(asdict(owner))):
            return WriteLock(path, owner)
        _break_if_stale(path)
        if time.monotonic() >= deadline:
            raise LockTimeoutError(path, timeout, _read_owner(path))
        time.sleep(retry)


def _break_if_stale(path: Path) -> None:
    break_path = path.with_name(f"{path.name}.break")
    if not _create_exclusive(break_path, str(os.getpid())):
        if _age_s(break_path) > _BREAK_GRACE_S:
            break_path.unlink(missing_ok=True)
        return
    try:
        # Re-checked under the break file: only a breaker ever deletes someone else's lock.
        if _is_stale(path):
            path.unlink(missing_ok=True)
    finally:
        break_path.unlink(missing_ok=True)


def _is_stale(path: Path) -> bool:
    owner = _read_owner(path)
    if owner is None:
        return _age_s(path) > _UNREADABLE_GRACE_S
    # A PID on another host can't be checked, so its lock is never judged stale.
    return owner.hostname == socket.gethostname() and not _is_alive(owner.pid)


def _is_alive(pid: int) -> bool:
    if pid <= 0:  # os.kill(0, …) would signal our own process group
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _create_exclusive(path: Path, content: str) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def _read_owner(path: Path) -> LockOwner | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LockOwner(
            pid=int(data["pid"]),
            hostname=str(data["hostname"]),
            acquired=str(data["acquired"]),
            token=str(data["token"]),
        )
    except OSError, ValueError, TypeError, KeyError:
        return None


def _age_s(path: Path) -> float:
    """Age from mtime; 0 if the file is gone, so a vanished file is never "old"."""
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return 0.0
