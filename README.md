# APM

A local-first project manager built for working with AI agents.

A project is a **nested graph of nodes**. You start with a big idea and break it down, level by level, until each piece is small enough to ship as one pull request. Claude Code agents do the breaking down and the building; you review what they propose before anything is written. The plan lives in its own git repo, so every change to it is a commit you can trace.

The design is in [plans.md](plans.md). §11 there says what is built and what isn't.

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

That puts an `apm` command on your PATH. To update later: `git pull`, build the UI again, then `uv tool install --reinstall .`.

## Daily use

In the repository you want to plan:

```bash
apm init
```

```bash
apm serve
```

`apm serve` opens the web UI in your browser. Only your machine can reach it, and only through the address it prints (it ends in a secret token; bookmark it).

In the UI:

- **Add a node** for the big idea, then open it (double-click) to work inside it.
- **Decompose** asks Claude to break a node into children. It reads the node and your code, a second agent reviews the result, and you get a **proposal**: dashed "ghost" nodes you can edit, trim, accept or reject. Nothing is written until you accept.
- **Implement** is offered on a node that has no children and nothing left to wait for. Claude builds it in its own git worktree, commits with a trailer naming the node, opens a pull request if `gh` is set up, and records the delivery on the node.
- **Ask** starts a conversation about a node. It changes nothing.
- The **Runs** tab of a node shows each agent run live, lets you stop it, and lets you send a follow-up message into the same conversation.
- Drag from one node to another to say the second needs the first.
- **Ready** in the top bar lists what can be worked on now.

When a pull request merges, `apm reconcile` (or the next `apm serve`) marks its node done.

### From the terminal

Everything the UI does is also a command. Nodes are named by their **handle**: the last 8 characters of their id, or any unambiguous shorter ending.

```bash
apm node add "Ship v1"
apm node add "Build the importer" --parent 1c0b9a44 --intent "Read the CSV export."
apm node ls                     # the plan as an outline
apm ready                       # what can be worked on now
apm run decomposer 1c0b9a44 -g "Slice by user-visible feature."
apm proposal show <id>          # then: apm proposal accept <id>
apm run implementer 7d2e0f11
apm node status 7d2e0f11 done
```

`apm --help` lists the rest.

### In your own Claude Code session

```bash
claude --plugin-dir "$(apm plugin-path)"
```

That session gets the plan's tools (`apm_get_graph`, `apm_ready`, `apm_create_nodes`, …), three skills (`apm-node-format`, `apm-decompose`, `apm-deliver`) and four subagents (`apm-decomposer`, `apm-critic`, `apm-implementer`, `apm-assistant`). Your own session may change the plan directly; agents started by APM may not.

## Configuration

`.apm/apm.yaml`, created by `apm init`:

```yaml
name: my-app
code:
  repos:
    my-app: { path: "..", default: true }
runner:
  max_concurrent: 2          # agent pipelines at once
  default_budget_usd: 2.00   # spending cap per run
  agents:
    apm-decomposer: { model: opus }
    apm-critic: { model: sonnet }
    apm-implementer:
      model: opus
      budget_usd: 5.00
      allowed_tools: ["Bash(uv run *)", "Bash(npm test*)"]
```

An agent can only use the tools it is allowed, because nobody is there to answer a permission prompt. The implementer starts with file editing, `git` and `gh pr`. **Add the commands that run your tests** under `allowed_tools`, or it cannot run them.

## What agents may do

| Agent | Reads | Writes |
|---|---|---|
| Decomposer, critic, assistant | the plan and your code | nothing: the decomposer returns a proposal |
| Implementer | the plan and your code | code in its own worktree; the status, delivery and description of its one node |

Every change to the plan is a commit in `.apm/` that records who made it (`APM-Actor`) and in which run (`APM-Run`).

## The plan repo

`.apm/` is its own git repository, ignored by your code repo. `apm link --remote <url>` gives it a remote, `apm push` and `apm pull` sync it, and `apm clone` fetches it into a fresh checkout of the code. You can edit its files by hand: run `apm commit` afterwards, which validates the result first.

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
| `src/apm/core/` | the graph, its rules, level views, reading and writing the plan, `mutate()` |
| `src/apm/cli.py` | the `apm` command |
| `src/apm/mcp/` | the MCP server agents use |
| `src/apm/runner/` | starting `claude` on a node and recording what it does |
| `src/apm/server/` | the HTTP and WebSocket API behind the UI |
| `src/apm/plugin/` | the Claude Code plugin: subagents and skills |
| `ui/` | the web UI |
| `tests/` | tests, mirroring `src/apm/`; agent runs are tested against a stand-in for `claude` |
