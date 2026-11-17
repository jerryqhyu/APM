"""Child process for the multi-process lock test: N rounds of lock → read-modify-write → unlock.

Usage: python lock_worker.py <apm_dir> <rounds>
"""

import sys
import time
from pathlib import Path

from apm.core.lock import acquire_write_lock


def main(apm_dir: Path, rounds: int) -> None:
    counter = apm_dir / "counter"
    trace = apm_dir / "trace"
    for _ in range(rounds):
        with acquire_write_lock(apm_dir, timeout=30, retry=0.005):
            # "+" and "-" around each hold: if two holders ever overlap, the trace shows "++".
            with trace.open("a") as f:
                f.write("+")
            n = int(counter.read_text()) if counter.exists() else 0
            time.sleep(0.002)
            counter.write_text(str(n + 1))
            with trace.open("a") as f:
                f.write("-")


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]))
