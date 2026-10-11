# APM

A local-first project manager built for working with AI agents. See [plans.md](plans.md) for the design and the phased implementation plan (§12).

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
| `src/apm/core/` | graph model, invariants, lifting, git I/O, index, `mutate()` (from P02) |
| `src/apm/cli.py` | the `apm` command: CLI, `serve`, `mcp` |
| `tests/` | tests, mirroring `src/apm/` |

The web UI (`ui/`, TypeScript) arrives in P13.
