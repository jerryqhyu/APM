import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from apm.core.lock import CACHE_DIR, LockOwner, LockTimeoutError, acquire_write_lock, lock_path

FAST = {"timeout": 0.2, "retry": 0.005}


def plant(apm_dir: Path, content: str | None = None, **owner: object) -> None:
    """Writes a lock file as if another process held it."""
    (apm_dir / CACHE_DIR).mkdir(parents=True, exist_ok=True)
    if content is None:
        fields = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired": "2026-10-11T00:00:00+00:00",
            "token": "someone-else",
            **owner,
        }
        content = json.dumps(fields)
    lock_path(apm_dir).write_text(content)


def dead_pid() -> int:
    """The PID of a process that has already exited."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def test_records_its_owner_and_release_removes_it(tmp_path: Path) -> None:
    lock = acquire_write_lock(tmp_path)
    on_disk = LockOwner(**json.loads(lock_path(tmp_path).read_text()))
    assert on_disk == lock.owner
    assert (on_disk.pid, on_disk.hostname) == (os.getpid(), socket.gethostname())
    lock.release()
    lock.release()
    assert not lock_path(tmp_path).exists()


def test_a_second_acquirer_waits_until_the_first_releases(tmp_path: Path) -> None:
    first = acquire_write_lock(tmp_path)
    order: list[str] = []

    def second() -> None:
        with acquire_write_lock(tmp_path, retry=0.005):
            order.append("second acquired")

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(0.05)
    order.append("first released")
    first.release()
    thread.join()
    assert order == ["first released", "second acquired"]


def test_times_out_on_a_live_holder_naming_it(tmp_path: Path) -> None:
    plant(tmp_path, pid=os.getpid())
    with pytest.raises(LockTimeoutError) as caught:
        acquire_write_lock(tmp_path, **FAST)
    assert caught.value.owner is not None
    assert caught.value.owner.pid == os.getpid()
    assert f"pid {os.getpid()} on {socket.gethostname()}" in str(caught.value)


def test_recovers_a_stale_lock_whose_owner_is_dead(tmp_path: Path) -> None:
    plant(tmp_path, pid=dead_pid())
    with acquire_write_lock(tmp_path, **FAST) as lock:
        assert lock.owner.pid == os.getpid()


def test_never_judges_a_lock_from_another_host_stale(tmp_path: Path) -> None:
    plant(tmp_path, pid=dead_pid(), hostname="some-other-host")
    with pytest.raises(LockTimeoutError):
        acquire_write_lock(tmp_path, **FAST)


def test_waits_on_an_unreadable_lock_then_recovers_it_once_abandoned(tmp_path: Path) -> None:
    plant(tmp_path, content="")
    with pytest.raises(LockTimeoutError):
        acquire_write_lock(tmp_path, **FAST)
    old = time.time() - 10
    os.utime(lock_path(tmp_path), (old, old))
    acquire_write_lock(tmp_path, **FAST).release()


def test_release_leaves_a_lock_that_is_not_ours_any_more(tmp_path: Path) -> None:
    lock = acquire_write_lock(tmp_path)
    plant(tmp_path, token="a-later-owner")
    lock.release()
    assert "a-later-owner" in lock_path(tmp_path).read_text()


def test_the_context_manager_releases_when_the_body_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"), acquire_write_lock(tmp_path):
        raise RuntimeError("boom")
    acquire_write_lock(tmp_path, **FAST).release()


def test_is_never_held_twice_across_processes_and_loses_no_updates(tmp_path: Path) -> None:
    worker = Path(__file__).with_name("lock_worker.py")
    processes, rounds = 6, 10
    children = [
        subprocess.Popen([sys.executable, str(worker), str(tmp_path), str(rounds)])
        for _ in range(processes)
    ]
    assert [child.wait(timeout=60) for child in children] == [0] * processes
    assert (tmp_path / "counter").read_text() == str(processes * rounds)
    assert (tmp_path / "trace").read_text() == "+-" * (processes * rounds)
