# APM

A local-first project manager built for working with AI agents. See [plans.md](plans.md) for the design.

**Status:** the core library (milestone M1 in plans.md §11) is implemented. The `apm` command only prints its version so far; the CLI commands, MCP server, web server and UI are still to come.

## Development

The backend is Python 3.14, managed with [uv](https://docs.astral.sh/uv/). uv installs the pinned Python (`.python-version`) and the locked dependencies (`uv.lock`) on first use.

```bash
uv sync                  # create .venv with the project and its dev tools
uv run apm --version     # run the CLI from source
uv run pytest            # tests (pytest + Hypothesis)
uv run ty check          # type check
uv run ruff check        # lint; `uv run ruff format` formats
uv build                 # build the wheel and sdist into dist/
```

| Path | Contents |
|---|---|
| `src/apm/core/` | the core library: see below |
| `src/apm/cli.py` | the `apm` command |
| `tests/` | tests, mirroring `src/apm/` |

### The core library

The in-memory graph is built on [networkx](https://networkx.org/): `Graph.tree` (containment) and `Graph.deps` (dependencies) are two `networkx.DiGraph`s over the same node ids.

| Module | Contents |
|---|---|
| `model`, `ids` | the node model; UUIDv7 ids and short handles |
| `ndjson`, `files`, `body`, `config` | reading and writing `graph.ndjson`, `nodes/<id>.md` and `apm.yaml` |
| `graph` | the in-memory `Graph` |
| `validate` | the invariants I1–I4, I6 and I7 |
| `status` | computed state: blocked, ready, progress |
| `lifting` | level views: lifted edges, boundary ports, entanglement (I5) |
| `git`, `lock` | the git wrapper and the cross-process write lock |
| `project` | `init_project()` and finding a project from `.apm-link` |
| `mutate`, `ops` | `mutate()` and the operations built on it; each makes one commit |

```python
from pathlib import Path

from apm.core.lifting import level_view
from apm.core.ops import NewNode, create_nodes, decompose, link
from apm.core.project import init_project, load_graph
from apm.core.status import ready_leaves

project = init_project(Path("my-app"))  # creates my-app/.apm, its own git repo
idea = create_nodes(project, [NewNode(title="Ship v1")]).created[0]
build, test = decompose(
    project, idea, [NewNode(title="Build", key="b"), NewNode(title="Test", depends_on_keys=["b"])]
).created

graph = load_graph(project)
print(ready_leaves(graph))  # [build]: Test waits on Build
print(level_view(graph, idea))  # the children of "Ship v1" and the edges between them
```
