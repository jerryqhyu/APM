"""Child process for the multi-process `mutate()` test: creates `count` nodes, one commit each.

Usage: python mutate_worker.py <code_dir> <name> <count>
"""

import sys
from pathlib import Path

from apm.core.ops import NewNode, create_nodes
from apm.core.project import open_project


def main(code_dir: Path, name: str, count: int) -> None:
    project = open_project(code_dir)
    for i in range(count):
        create_nodes(project, [NewNode(title=f"{name}-{i}")])


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]))
