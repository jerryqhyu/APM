# APM

A local-first project manager built for working with AI agents.

A project is a **nested graph of nodes**. You start with a big idea and break it down, level by level, until each piece is small enough to ship as one pull request. You do it in conversation with Claude: it reads your code, shapes the plan with you, and builds the pieces. The plan stays on your machine, and every change to it is recorded.

- **[User guide](docs/user-guide.md)**: everything you can do with it, step by step.
- **[plans.md](plans.md)**: the design, and what is built (§11).

## Install

You need [uv](https://docs.astral.sh/uv/), git, Node with pnpm (to build the web UI once), and the [Claude Code](https://claude.com/claude-code) CLI signed in.

```bash
git clone https://github.com/jerryqhyu/APM.git
```

```bash
cd APM/ui && pnpm install && pnpm build && cd ..
```

```bash
uv tool install .
```

## Use

In the repository you want to plan:

```bash
apm init
```

```bash
apm serve
```

`apm serve` opens the web UI. Only your machine can reach it, and only through the address it prints (it ends in a secret token; bookmark it).

- **Talk to Claude** in the conversation on the right. Ask where things stand, plan a feature together, or have it reshape the plan: add, edit, link, move, merge and delete nodes. It goes turn by turn, and when Claude has a question or needs permission for something, a card appears for you to answer.
- **Change the graph yourself** with the actions panel: every operation is a button, for one node, several, or the whole plan.
- **Decompose** a node to get a reviewed proposal you can edit before anything is written.
- **Implement** a ready node: Claude builds it in its own git worktree, opens a pull request if `gh` is set up, and records the delivery on the node. On a feature, **Review all tasks** and **Implement all** work on every task inside it at once.
- **Feature reports**: once a feature's review passes, Claude writes a report of it for people who weren't there (what it is and why, the decisions you made and your reasons, how it was built) from the record APM keeps, and it lands with the feature as `docs/features/….md`, linked from its pull request. **Write report** does it by hand.
- **Eager mode** builds a whole feature in one run, then splits it into one pull request per task, chained so each shows only its own part, and rebuilds the later ones when an earlier one changes in review.
- **Settings…** sets the task size limit, feature reports and what `apm serve` does on its own, for the project or for one feature. They live in `.apm/apm.yaml`, which keeps your comments.
- **Permissions** are yours to set per run: Auto (the default), Ask, Accept edits, Plan first, or Don't ask.

Everything is also a terminal command (`apm --help`), and your own Claude Code session can use the plan with `claude --plugin-dir "$(apm plugin-path)"`. The [user guide](docs/user-guide.md) covers all of it.

## Where the plan lives

In `.apm/` inside your repository: its own small git repository, on your machine only. Your code repository ignores it and nothing is uploaded.

## Not built yet

- **Playback:** replaying the plan's history on a timeline.
- **A search index:** search scans the files, which is fine for hundreds of nodes.
- Issue-tracker sync, multiple code repos per project, multiple users.

## Development

The backend is Python 3.14, managed with uv. The in-memory graph is built on [networkx](https://networkx.org/). The UI is TypeScript (React, React Flow, elkjs) under `ui/`.

```bash
uv sync                  # create .venv with the project and its dev tools
uv run pytest            # tests (pytest + Hypothesis)
uv run ty check          # type check
uv run ruff check        # lint; `uv run ruff format` formats
uv run apm --version     # run the CLI from source
```

```bash
cd ui && pnpm install && pnpm build    # writes the UI into src/apm/server/static/
```

For UI work, run `uv run apm serve --dev --no-open` in a project and `pnpm dev` in `ui/`, then open the Vite address with the same `?token=…`.

| Path | Contents |
|---|---|
| `src/apm/core/` | the graph, its rules, level views, reading and writing the plan, the operations |
| `src/apm/cli.py` | the `apm` command |
| `src/apm/mcp/` | the MCP server agents use |
| `src/apm/runner/` | starting `claude`, relaying its questions, recording what it does |
| `src/apm/server/` | the HTTP and WebSocket API behind the UI |
| `src/apm/plugin/` | the Claude Code plugin: subagents and skills |
| `ui/` | the web UI |
| `docs/` | the user guide |
| `tests/` | tests, mirroring `src/apm/`; agent runs are tested against a stand-in for `claude` |
