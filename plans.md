# APM — Design Plan

**Status:** v5 draft, scoped for v1 · phased implementation plan in §12 · **Last updated:** 2026-10-11

APM is a local-first project manager built for working with AI agents. A project is a **nested graph of nodes**: you start with a big idea, then break it down with Claude agents, level by level, until each piece is small enough to ship as a commit or PR. The plan lives in its own git repo, so every change to it can be traced and **played back** from the project's first node onward.

v1 = a Python backend with a TypeScript web UI, one user on one machine, one code repo, Claude CLI integration.

---

## 1. Decisions

| Topic | Decision | Why |
|---|---|---|
| Language | **Python 3.14** for the backend (core, CLI, server, MCP), managed with **uv**; TypeScript only for the web UI | The standard library covers UUIDv7, SQLite with FTS5 and subprocess git; first-class MCP and Claude Agent SDKs; fast, strict tooling (uv, ruff, ty). The UI stays TypeScript because it runs in the browser. Installed with `uvx` / `uv tool install`. Replaces TypeScript-everywhere, before any core code merged (P01b) |
| Graph model | Compound graph: containment **tree** + dependency **DAG**; cross-level edges stored once and *lifted* for display | Unlimited depth, zoom into any node, no hand-copying edges between levels |
| Plan storage | Separate git repo at `.apm/`, ignored by the code repo and linked to it by a committed `.apm-link` | No CI runs on plan commits; works with a protected `main`; history holds only plan changes |
| Source of truth | `graph.ndjson` (structure + metadata) + `nodes/<id>.md` (prose only) | Each field lives in exactly one place, so the two can't drift |
| IDs | UUIDv7; the short handle is the **last** 8 hex chars | Sorts by creation time; new nodes append at the end of the file, so diffs stay small |
| Local DB | SQLite (the standard library's `sqlite3`) in `.apm/.cache/`, not committed | Queries, replay frames, run logs. Can be deleted at any time |
| History | Git commits with structured trailers, indexed into SQLite | Git records the state and trailers record the intent, with no custom event log to maintain |
| Writes | `core.mutate()` under a file lock, callable from any process | No daemon required; writes happen one at a time, so updates aren't lost |
| Agents | Server spawns `claude` CLI; agents call back through the APM MCP | The server controls runs; the MCP is how agents read and write data |
| Planning writes | Agents return **proposals** (`--json-schema`); nothing is committed until you accept | Rejected ideas stay out of the history; nothing is half-written if a run fails |
| Packaging | Agents + skills + MCP ship as one Claude Code plugin | The same plugin serves spawned runs and your own terminal sessions |
| Scope | Single user, single machine, one code repo (config already shaped for many repos) | Fastest route to a working slice |

---

## 2. Concepts

### 2.1 Nodes and levels

- Each node has **at most one parent** (`parent: null` = top level). Containment is a strict tree, so subtasks are never shared.
- A "level" is the set of children of one parent. You zoom into a node to see its children as their own DAG.
- There is no special root node. You can add a node at any level, at any time.
- Depth is computed, never stored.
- `kind`: `work` (default) | `decision`.

### 2.2 Dependencies and lifting

`depends_on` can point at **any** node, including one under a different parent. The edge is stored once, on the node where it's true.

To show the children of node `R`, each edge `(u → v)` is mapped as follows:

- `a` = the ancestor of `u` (or `u` itself) that is a child of `R`; `b` = the same for `v`.
- `a ≠ b` → draw `a → b` (merge duplicates and label the count, e.g. "3 underlying").
- `a = b` → the edge is internal to `a`; hide it.
- `u` or `v` is outside `R` → draw a **boundary port** (a stub at the canvas edge, labelled with the outside node).

### 2.3 Status

Stored: `todo | in_progress | done | dropped`.

Computed (never stored, so it can't go stale):

- **blocked**: `todo` and at least one dependency is not `done`.
- **ready**: `todo` and not blocked.
- **progress** (parents): done leaves ÷ leaves that aren't dropped, across the whole subtree.
- A node that depends on a `dropped` node is flagged. It is **not** treated as unblocked.

### 2.4 Invariants

| # | Invariant | On violation |
|---|---|---|
| I1 | Containment is a tree (one parent, no cycles, parent exists) | hard error |
| I2 | `depends_on` is acyclic over all nodes | hard error |
| I3 | A node cannot depend on its own ancestor or descendant | hard error |
| I4 | Every `depends_on` target exists | hard error (deletes remove incoming edges in the same op) |
| I5 | Lifted edges are acyclic at every level | **warning: "entanglement"**, usually a sign the breakdown needs rethinking |
| I6 | A `done` parent has no unfinished children | warning |
| I7 | Only leaves carry `delivery` | hard error |

`mutate()` runs I1–I4 and I7 before every commit. The UI and `apm check` report I5–I6.

---

## 3. Storage

### 3.1 Layout

```
<code-repo>/
  .apm-link                 committed in the CODE repo: { "path": ".apm", "remote": null }
  .gitignore                contains ".apm/"
  .apm/                     ← separate git repo, branch main
    apm.yaml
    graph.ndjson
    nodes/<uuid>.md
    .gitignore              contains ".cache/"
    .cache/                 apm.db, write.lock, server.json
```

`nodes/` is flat. Moving a node to a new parent changes one line in `graph.ndjson` and renames no files.

### 3.2 `graph.ndjson`: structure and metadata

One node per line, sorted by `id`. Keys are always written in the same order; null and empty fields are left out.

```json
{"id":"0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44","parent":"0192f2c8-1a0d-7e33-8c41-5b2a9d0e7f10","title":"Stream agent output to the UI","kind":"work","status":"in_progress","depends_on":["0192e9d0-…"],"delivery":{"repo":"app","branch":"apm/1c0b9a44-stream-output","commits":["a1b2c3d"],"pr":"https://github.com/jerry/app/pull/12"}}
```

Fields: `id, parent, title, kind, status, depends_on, delivery, external, variant_of`.

- No `created`: UUIDv7 already contains the creation timestamp.
- No `updated`: git history has it.
- No `session`: Claude session IDs only mean something on one machine, so they're kept in SQLite.
- `external` is reserved for issue sync: `{ provider, id, synced_at, synced_hash }`.
- `variant_of` is reserved for competing breakdowns (v2).
- `delivery.pr` is the pull request URL.

Format rules (the serializer's output is byte-stable, so the same graph always produces the same file):

- Key order is the field order above; nested objects follow the same rules.
- `kind` and `status` are always written. `depends_on` is sorted and de-duplicated.
- Hand-written lines may be in any order, use `null` for absent fields, start with a byte-order mark, and leave out `kind`/`status` (defaults: `work`/`todo`). The next write puts them in canonical form.
- Unknown keys are errors, so a typo can't be silently dropped. IDs must be lowercase UUIDv7s; titles must be one non-blank line.
- The writer checks every node against the same rules as the reader and refuses to write one it couldn't read back, so a bad value can never make the file unloadable.

### 3.3 `nodes/<uuid>.md`: prose only

Plain markdown with no frontmatter. You and agents can edit these files freely without any risk of breaking the graph structure.

```markdown
## Intent
Why this exists, and what outcome it's for.

## Acceptance criteria
- [ ] …

## Notes
```

### 3.4 `apm.yaml`

```yaml
name: my-app
code:
  repos:
    app: { path: "..", remote: "git@github.com:jerry/app.git", default: true }
runner:
  max_concurrent: 2
  default_budget_usd: 2.00
  agents:
    apm-decomposer: { model: opus }
    apm-critic:     { model: sonnet }
    apm-implementer: { model: opus, budget_usd: 5.00, allowed_tools: ["Bash(git *)", "Bash(npm test*)"] }
```

### 3.5 Commit format (plan repo)

Each semantic operation produces exactly one commit:

```
decompose 1c0b9a44: 4 children

APM-Op: decompose
APM-Nodes: <uuid>,<uuid>,<uuid>,<uuid>,<uuid>
APM-Actor: agent:apm-decomposer        # or user
APM-Run: <run-id>                      # optional
```

Ops: `create, update, move, delete, link, unlink, status, deliver, decompose, edit-body`.

Commits are made through the `git` CLI (`subprocess` with an argument list, never a shell), so your identity, signing, and hooks apply.

### 3.6 Code-repo trailer

Every delivery commit in the code repo carries `APM-Node: <full-uuid>`. This is how code links back to the plan, and it's what the reconciler reads.

### 3.7 IDs and handles

- Stored IDs are full UUIDv7 values.
- The **handle** is the last 8 hex chars (`1c0b9a44`). All CLI and MCP inputs accept an unambiguous handle suffix of at least 4 hex chars, or a full ID (with or without hyphens, any case).
- Those 8 chars are 32 random bits, so two nodes can share them (about a 1% chance somewhere in a 10,000-node project). Where that happens, both are displayed with as many extra chars as it takes to tell them apart.
- ⚠ Do **not** use the leading characters. In UUIDv7 those are the timestamp, so nodes created within about a minute of each other share their first 8 characters.

---

## 4. Local index (SQLite)

`.apm/.cache/apm.db`, using the standard library's `sqlite3` (FTS5 is required; opening the database checks for it). WAL mode.

```sql
nodes(id PK, parent, title, kind, status, depth)
edges(src, dst, PRIMARY KEY(src, dst))
closure(ancestor, descendant, depth)          -- containment, incl. self at depth 0
nodes_fts                                     -- FTS5 over title + body
frames(seq PK, sha, ts, op, actor, node_ids)  -- one row per plan commit
meta(key PK, value)                           -- last_indexed_sha, schema_version
runs(id PK, node_id, agent, session_id, status, started, ended, cost_usd, error)
run_events(run_id, seq, ts, json)             -- raw stream-json lines
proposals(id PK, node_id, run_id, json, status, created)
```

The `closure` table makes subtree queries and lifting lookups ("which child of R contains u") a single indexed query each.

**Rebuild rule:** everything above `runs` can be rebuilt from git with `apm reindex`. Deleting `.cache/` loses only run logs and pending proposals, never anything about the project itself.

**Keeping it current:** before each read, compare `.apm` HEAD with `last_indexed_sha`. If HEAD has moved forward, index only the new commits. If history was rewritten (the old HEAD isn't an ancestor), rebuild from scratch.

---

## 5. History and playback

Build the frames in one pass:

```bash
git -C .apm log --reverse --format='%H%x00%ct%x00%(trailers:key=APM-Op,valueonly)%x00…' -- graph.ndjson
git -C .apm show <sha>:graph.ndjson        # one blob per frame
```

- Frames go into `frames`. The scrubber works entirely from SQLite and loads blobs lazily, holding a small LRU of parsed graphs.
- Body edits are shown in the node panel's history (`git log -- nodes/<id>.md`); they don't create new frames.
- **Delivery overlay:** read the code repo's log for commits with an `APM-Node` trailer, merge them into the timeline by timestamp, and show each one as a marker on its node.
- Playback is read-only. v2 may add "restore from here", which would add a new commit rather than rewrite history.

---

## 6. Components

```
                ┌──────────────────── apm.core ──────────────────────┐
                │ graph model · lifting · invariants · mutate()      │
                │ git I/O · indexer · SQLite                         │
                └────▲──────────────────▲───────────────────▲────────┘
                     │                  │                   │
               apm CLI            apm serve             apm mcp (stdio)
            (humans/scripts)   HTTP+WS · runner      ◀── spawned agents
                                    ▲                ◀── your own `claude`
                                    │
                                 web UI
```

### 6.1 Layout (one Python package, one `apm` command)

```
src/apm/core/    graph, invariants, lifting, git, index, mutate (no web or CLI dependencies)
src/apm/cli.py   the `apm` command (Typer): every CLI command, plus `serve` and `mcp`
src/apm/server/  FastAPI app (HTTP + WebSocket) and the runner, served by uvicorn
src/apm/mcp/     stdio MCP server on the official `mcp` SDK
tests/           pytest + Hypothesis, mirroring src/apm/
ui/              TypeScript: vite + react + @xyflow/react + elkjs; its build ships inside the wheel
plugin/          Claude Code plugin (agents, skills, .mcp.json)
```

- **Backend:** Python 3.14, one distribution with an `apm` console script, managed with **uv** (committed `uv.lock`, `uv_build` backend, src layout). **ruff** lints and formats, **ty** type-checks, **pytest** + **Hypothesis** test. Names follow PEP 8: snake_case for modules, functions, variables and fields, PascalCase for classes; ruff's pep8-naming rules enforce it.
- **Libraries:** pydantic (validating `graph.ndjson` lines, `apm.yaml`, and API and MCP inputs), PyYAML, Typer, FastAPI + uvicorn, and the `mcp` SDK. The standard library covers UUIDv7 (`uuid.uuid7()`), SQLite with FTS5 (`sqlite3`) and git (`subprocess`).
- **Core is synchronous** and uses `pathlib` throughout. The server calls it from a worker thread and runs agent subprocesses with `asyncio`.
- **UI:** TypeScript in its own pnpm workspace under `ui/` (Biome, Vitest), because it runs in the browser. It talks to the backend only over HTTP and WebSocket (§6.3), so the two sides share no code.

### 6.2 Write concurrency

`mutate(op, fn)`:

1. Acquire `.cache/write.lock` (created with O_EXCL, marked stale if the owning PID is dead).
2. Reload the graph from disk while holding the lock.
3. Apply `fn`, check the invariants, and serialize `graph.ndjson` and any bodies.
4. `git add` + `git commit` with trailers.
5. Update the index, release the lock, and emit `graph.changed`.

Any process can call `mutate()`. The server doesn't have to be running for the CLI or MCP to work.

### 6.3 Server

- Binds to **127.0.0.1 only**, and every request must carry a random token stored in `.cache/server.json` (the UI gets it from the URL `apm serve` prints). The server can launch agents that run tools, so a web page from any other site must not be able to POST to it.
- It also checks the `Origin` header.

```
GET    /api/graph?root=<id>&at=<sha>        level view (lifted edges, ports, warnings)
GET    /api/nodes/:id                       node + body + history + runs
POST   /api/nodes · PATCH /api/nodes/:id · POST /api/nodes/:id/move · DELETE /api/nodes/:id
POST   /api/edges · DELETE /api/edges
POST   /api/runs            { agent, node_id, guidance? }
DELETE /api/runs/:id        cancel
POST   /api/proposals/:id/accept   { edits? }
POST   /api/proposals/:id/reject
GET    /api/frames
WS     /ws   graph.changed · run.event · run.finished · proposal.created · index.progress
```

### 6.4 MCP (`apm mcp`, stdio)

Calls core directly (no HTTP), so it works even when the server isn't running.

| Tool | Notes |
|---|---|
| `apm_get_graph(root?, depth?)` | Returns a **compact outline**, not JSON: `1c0b9a44 [work/todo·ready] Stream agent output → 7d2e0f11` |
| `apm_get_node(id)` | metadata + body |
| `apm_search(query)` | FTS |
| `apm_ready(root?)` | ready leaves: "what can be worked on now" |
| `apm_create_nodes(nodes[])` | batch; one commit |
| `apm_update_node(id, {title?, kind?, body?})` | |
| `apm_move_node(id, parent)` | |
| `apm_link / apm_unlink(from, to)` | |
| `apm_set_status(id, status, note?)` | |
| `apm_attach_delivery(id, {branch?, commits?, pr?})` | leaves only |

**Finding the project:** use `APM_PROJECT` if it's set; otherwise walk up from cwd looking for `.apm-link`. (Implementer worktrees don't contain `.apm/`, so spawned runs always set `APM_PROJECT`.)

**Scoping (`APM_RUN_SCOPE`):**
- `read`: all write tools are refused. Used by the decomposer and critic.
- `node:<id>`: only `set_status` / `attach_delivery` / `update_node(body)` on that node. Used by the implementer.
- `project`: no restriction. Used by the reconciler and by interactive sessions.

---

## 7. Claude CLI integration

### 7.1 Runner

The runner lives in `apm serve`, and the CLI calls it through `apm run`. Each run:

1. Inserts a `runs` row and generates a session UUID.
2. Writes a per-run MCP config (injecting `APM_PROJECT` and `APM_RUN_SCOPE`).
3. Spawns:

```bash
claude -p "$PROMPT" \
  --agent apm-decomposer \
  --plugin-dir "$APM_PLUGIN" \
  --mcp-config "$RUN_MCP_JSON" --strict-mcp-config \
  --setting-sources project \
  --permission-prompts none \
  --allowedTools "mcp__apm__apm_get_graph,mcp__apm__apm_get_node,Read,Grep,Glob" \
  --json-schema "$DECOMPOSITION_SCHEMA" \
  --max-budget-usd 2.00 \
  --model opus \
  --session-id "$SESSION_UUID" \
  --output-format stream-json --verbose --include-partial-messages
```

4. Streams each stdout line → `run_events` → WS `run.event`.
5. When the run exits, parses the final `result` event and saves structured output as a **proposal**.

Limits: `max_concurrent` runs in total and one active run per node. Cancel = SIGTERM, then SIGKILL after 5s.

**Why each flag is there:**
- The MCP is passed with `--mcp-config` rather than through the plugin's `.mcp.json`, so each run can inject its own env vars. `--strict-mcp-config` keeps your personal MCP servers out of runs.
- `--permission-prompts none` means a headless run is denied instead of hanging forever on a prompt.

**To check at M3:**
- whether agents from a `--plugin-dir` need a namespaced `--agent` name
- whether `--json-schema` output arrives in the `stream-json` `result` event
- whether `--strict-mcp-config` also removes the plugin's own MCP entry (it should, and that's what we want)

### 7.2 Proposals (planning never auto-commits)

```
decomposer ──json──▶ critic ──issues?──▶ decomposer (1 revision max) ──▶ proposal
                                                                          │
                                   UI shows ghost children ◀──────────────┘
                                   you edit / trim / accept ──▶ mutate(decompose) → 1 commit
```

Decomposition schema: new nodes don't have IDs yet, so they use temporary keys.

```json
{
  "rationale": "string",
  "children": [
    { "key": "a", "title": "string", "kind": "work|decision",
      "intent": "string", "acceptance": ["string"],
      "depends_on_keys": ["b"], "depends_on_ids": ["<existing uuid>"] }
  ],
  "warnings": ["string"]
}
```

When a proposal is accepted, the server assigns UUIDv7s, resolves the keys, runs the invariant checks, and commits.

v1: decomposing a node that already has children is **additive only**. It can add children and edges, and it cannot modify or remove existing ones.

### 7.3 Sessions

`runs.session_id` records every Claude session for each node. "Continue" on a node resumes its most recent session with `--resume <id>`. `--fork-session` is reserved for v2 variants.

### 7.4 Implementer

- **Only offered for ready leaves** (every dependency done).
- Spawned with `-w apm/<handle>-<slug>` so it works in its own worktree and your checkout is never touched.
- Scope `node:<id>`.
- Commits carry `APM-Node:`. It opens a PR if `gh` is available.
- Before exiting it calls `apm_attach_delivery` and `apm_set_status(in_progress)`. The node becomes `done` when you mark it, or when the reconciler finds the PR merged.

---

## 8. Agents and skills (plugin)

```
plugin/
  .claude-plugin/plugin.json
  agents/
    apm-decomposer.md     node → proposal (read-only scope, json-schema)
    apm-critic.md         proposal → issues: gaps, overlap, untestable criteria,
                          near-duplicates elsewhere in the graph, entanglement
    apm-implementer.md    ready leaf → worktree → commits → PR → attach delivery
    apm-reconciler.md     code-repo log (APM-Node trailers, PR state) → status updates
  skills/
    apm-node-format/      node fields, body sections, handles, invariants
    apm-decompose/        what a good breakdown looks like: 3–7 children, no overlap,
                          explicit deps, testable acceptance, prefer sibling edges
    apm-deliver/          branch naming, trailer format, PR body template linking the node
  .mcp.json               { "apm": { "command": "apm", "args": ["mcp"] } }  (interactive use)
```

In interactive use, `claude --plugin-dir <apm>/plugin` gives your own terminal session the same tools and skills.

---

## 9. UI

- **Level view:** shows the children of the current node (the top level to start), laid out as a layered DAG with elkjs and drawn with React Flow. Double-click a node to go into it; a breadcrumb takes you back up.
- **Node card:** handle, title, status (including computed blocked/ready), a child-progress bar, and a small "has children" marker.
- **Edges:** lifted edges show a count badge. Boundary ports appear at the canvas edges. Any entanglement warning shows in a banner.
- **Node panel:**
  - markdown body editor
  - dependencies
  - delivery (branch, commits, PR)
  - history
  - runs, with a live streaming transcript
  - actions: *Add child · Decompose (with optional guidance) · Implement (ready leaves) · Set status*
- **Proposals:** shown as ghost nodes and edges. You can edit or remove any of them before choosing Accept or Reject.
- **Playback:** a timeline scrubber at the bottom. Scrubbing puts the view in read-only mode; delivery markers from the code repo sit along the timeline.

---

## 10. CLI (v1)

```
apm init                         create .apm/, .apm-link, gitignore entry, first commit
apm link --remote <url>          set the plan repo remote and record it in .apm-link
apm clone                        clone the plan repo named in .apm-link (for a fresh checkout)
apm serve [--port]               server + UI; prints a tokenized URL
apm mcp                          stdio MCP server
apm node add <title> [--parent h] [--kind decision]
apm node ls [--root h] [--depth n]     outline view
apm node show <h> · mv <h> <parent|--top> · rm <h> · status <h> <s> · edit <h>
apm link-dep <from> <to> · apm unlink-dep <from> <to>
apm ready [--root h]
apm run <agent> <h> [--guidance "..."]
apm proposal ls · show <id> · accept <id> · reject <id>
apm check                        validate invariants, report warnings
apm reindex
apm push · apm pull              git push/pull for .apm
```

`apm node edit <h>` opens the body in `$EDITOR` and commits it as `edit-body` when you close the editor. If you edit files directly in `.apm/`, run `apm check` and then `apm commit`: it validates the changes and commits the dirty files as `edit-body` (or `update` if `graph.ndjson` changed).

---

## 11. Build plan

| M | Deliverable | Exit criteria |
|---|---|---|
| **M1** | `core`: model, ndjson/md I/O, invariants, lifting, `mutate()` + lock, git commits with trailers | Property tests (Hypothesis) confirm I1–I4 hold across random op sequences; lifting and entanglement tests pass |
| **M2** | `apm` CLI + `apm mcp` + plugin skeleton + skills | **Dogfood: plan APM itself** with the CLI and an interactive `claude` session |
| **M3** | `apm serve`: runner, decomposer + critic, proposals, SQLite runs | `apm run apm-decomposer <h>` → proposal → `apm proposal accept` → one commit; the three M3 CLI checks in §7.1 are answered |
| **M4** | UI: level view, drill-down, node panel, live run stream, proposal review | Planning a feature entirely in the UI, start to finish |
| **M5** | Indexer frames + playback scrubber + delivery overlay | Replaying the APM plan from its first commit |
| **M6** | `apm-implementer` (worktrees, trailers, `gh` PR) | A ready leaf becomes a PR that links back to its node |
| **M7** | `apm-reconciler` | A merged PR moves its node to `done` with no manual step |

Each milestone is split into PR-sized phases in §12.

---

## 12. Implementation phases

Each phase is **one branch → one PR into `main`**, or, when it's too big for that, one PR per child phase (§12.1). Phases are small enough to review in one sitting, and each leaves `main` green and usable. A milestone is complete when its last phase merges and the milestone's exit criteria (§11) are met.

### 12.1 Conventions (every phase)

- **Branch:** `phase/NN-<slug>` (e.g. `phase/02-model-io`); a child phase uses its full number (`phase/02.1-model-ids`).
- **Size: at most 300 lines per PR**, counted as added lines excluding comments, blank lines, docs and generated files (lockfile). Fixtures and tests count.
- **Too big → decompose, the APM way.** A phase that won't fit is broken into child phases (`P02.1`, `P02.2`, …), exactly as APM breaks down a node: each child gets its own scope, real dependencies and exit criteria, and ships as its own PR. A child that still won't fit is broken down again. The parent has no PR of its own; it's done when all its children are, and its exit criteria are checked then.
- **Stacked PRs:** when children must land in sequence, each PR targets the branch of the one before it. Merge them in order with a **merge commit** (not squash), so the stack stays intact and GitHub retargets the next PR to `main` automatically.
- **PR title:** `P<NN>: <phase title>`. **PR body:** goal, scope delivered, the phase's exit criteria as a ticked checklist, anything deferred, and follow-ups.
- **CI must pass:** `ruff format --check`, `ruff check`, `ty check` and `pytest` (from P01b onward), plus the UI's own lint, typecheck and tests once `ui/` exists (P13.1).
- **Python style:** PEP 8 names (snake_case modules, functions, variables and fields; PascalCase classes), type hints on everything `ty` checks, `pathlib` for paths, frozen dataclasses for value types and pydantic only at input boundaries (files, API, MCP).
- **Keep the plan true:** each PR updates its row in the tracker (§12.2), and edits the design sections if the implementation diverged from them. The plan doc is never allowed to drift from the code.
- **No dead surface:** a command, endpoint or tool is registered only once it works. Nothing ships as a "not implemented" stub.
- **Tests mirror the code:** pytest tests under `tests/` mirror `src/apm/` (`tests/core/test_ids.py` tests `apm/core/ids.py`); shared Hypothesis strategies live in `tests/strategies.py`. End-to-end tests run the `apm` command against throwaway git repos in a temp dir.
- From **P09** onward, phase status is also tracked in APM itself (dogfooding). This file stays the design reference.

### 12.2 Tracker

| Phase | Title | M | Depends on | Status | PR |
|---|---|---|---|---|---|
| P00 | Phased implementation plan (this doc) | — | — | done | [#1](https://github.com/jerryqhyu/APM/pull/1) |
| P01 | Workspace scaffold + CI *(TypeScript; replaced by P01b)* | M1 | P00 | done | [#2](https://github.com/jerryqhyu/APM/pull/2) |
| P01b | Python backend scaffold + CI | M1 | P01 | todo | |
| P02 | Node model, IDs and file I/O *(parent: done when P02.1–P02.5 are)* | M1 | P01b | in progress | ~~[#3](https://github.com/jerryqhyu/APM/pull/3)~~ (split) |
| ↳ P02.1 | Node model + IDs and handles | M1 | P01b | in review | [#4](https://github.com/jerryqhyu/APM/pull/4) |
| ↳ P02.2 | Plan-repo paths, atomic writes, bodies | M1 | P02.1 | in review | [#5](https://github.com/jerryqhyu/APM/pull/5) |
| ↳ P02.3 | `apm.yaml` config | M1 | P02.2 | in review | [#6](https://github.com/jerryqhyu/APM/pull/6) |
| ↳ P02.4 | `graph.ndjson` parser | M1 | P02.1 | in review | [#7](https://github.com/jerryqhyu/APM/pull/7) |
| ↳ P02.5 | `graph.ndjson` serializer + graph file I/O | M1 | P02.2, P02.4 | in review | [#8](https://github.com/jerryqhyu/APM/pull/8) |
| P03 | Invariants and computed status *(parent: done when P03.1–P03.4 are)* | M1 | P02 | todo | |
| ↳ P03.1 | In-memory `Graph` + test generator | M1 | P02 | in review | [#10](https://github.com/jerryqhyu/APM/pull/10) |
| ↳ P03.2 | `validate()` + structural invariants (I1, I4, I7) | M1 | P03.1 | todo | |
| ↳ P03.3 | Dependency invariants (I2, I3) + I6 warning | M1 | P03.2 | todo | |
| ↳ P03.4 | Computed status | M1 | P03.1 | todo | |
| P04 | Lifting and level views *(parent: done when P04.1–P04.3 are)* | M1 | P03 | todo | |
| ↳ P04.1 | Lifted edges | M1 | P03.1 | todo | |
| ↳ P04.2 | Boundary ports | M1 | P04.1 | todo | |
| ↳ P04.3 | Entanglement (I5) + `check_all()` | M1 | P04.1, P03.3 | todo | |
| P05 | `mutate()`, write lock, git commits, `init` *(parent: done when P05.1–P05.6 are)* | M1 | P03 | todo | |
| ↳ P05.1 | Git wrapper | M1 | P01b | in review | [#11](https://github.com/jerryqhyu/APM/pull/11) |
| ↳ P05.2 | Write lock | M1 | P02.2 | in review | [#12](https://github.com/jerryqhyu/APM/pull/12) |
| ↳ P05.3 | `init_project()` + project discovery | M1 | P05.1, P02.3, P02.5 | todo | |
| ↳ P05.4 | `mutate()` core | M1 | P05.1, P05.2, P05.3, P03.3 | todo | |
| ↳ P05.5 | Node ops: create, update, status, deliver, edit-body | M1 | P05.4 | todo | |
| ↳ P05.6 | Structure ops: move, delete, link, unlink + op property test | M1 | P05.5 | todo | |
| P06 | SQLite index + FTS *(parent: done when P06.1–P06.3 are)* | M2 | P05 | todo | |
| ↳ P06.1 | SQLite wrapper + migrations | M2 | P05.3 | todo | |
| ↳ P06.2 | Graph tables + freshness | M2 | P06.1, P05.4 | todo | |
| ↳ P06.3 | FTS5 search | M2 | P06.2 | todo | |
| P07 | `apm` CLI *(parent: done when P07.1–P07.5 are)* | M2 | P04, P06 | todo | |
| ↳ P07.1 | CLI framework, outline, `init`, `node ls` | M2 | P05.3, P03.4 | todo | |
| ↳ P07.2 | Node commands | M2 | P07.1, P05.6 | todo | |
| ↳ P07.3 | `node edit`, `check`, `commit` | M2 | P07.2, P04.3 | todo | |
| ↳ P07.4 | `ready` + `reindex` | M2 | P07.3, P06.3 | todo | |
| ↳ P07.5 | Plan-repo remote: `link --remote`, `clone`, `push`, `pull` | M2 | P07.1 | todo | |
| P08 | `apm mcp` + plugin skeleton + skills *(parent: done when P08.1–P08.4 are)* | M2 | P07 | todo | |
| ↳ P08.1 | MCP server + read tools | M2 | P07.1, P06.3 | todo | |
| ↳ P08.2 | MCP write tools | M2 | P08.1, P05.6 | todo | |
| ↳ P08.3 | `APM_RUN_SCOPE` enforcement | M2 | P08.2 | todo | |
| ↳ P08.4 | Plugin skeleton + skills | M2 | P08.1 | todo | |
| P09 | Dogfood: plan APM in APM | M2 | P08 | todo | |
| P10 | `apm serve`: HTTP/WS core and security *(parent: done when P10.1–P10.4 are)* | M3 | P07 | todo | |
| ↳ P10.1 | Server core + security | M3 | P07.1, P04.3 | todo | |
| ↳ P10.2 | Node read endpoint | M3 | P10.1 | todo | |
| ↳ P10.3 | Write endpoints | M3 | P10.2, P05.6 | todo | |
| ↳ P10.4 | WS `graph.changed` | M3 | P10.1 | todo | |
| P11 | Runner: spawning `claude`, run logs, cancel *(parent: done when P11.1–P11.4 are)* | M3 | P10 | todo | |
| ↳ P11.1 | Run tables, stream-json parser, fake `claude` | M3 | P06.1 | todo | |
| ↳ P11.2 | Spawn a run | M3 | P11.1, P08.3, P10.4 | todo | |
| ↳ P11.3 | Limits, cancel, resume | M3 | P11.2 | todo | |
| ↳ P11.4 | Runs API + `apm run` | M3 | P11.3, P10.2 | todo | |
| P12 | Decomposer, critic and proposals *(parent: done when P12.1–P12.4 are)* | M3 | P11 | todo | |
| ↳ P12.1 | Schemas + agent definitions | M3 | P08.4 | todo | |
| ↳ P12.2 | Proposal pipeline | M3 | P12.1, P11.3 | todo | |
| ↳ P12.3 | Accepting a proposal | M3 | P12.1, P05.6 | todo | |
| ↳ P12.4 | Proposals API + CLI | M3 | P12.2, P12.3, P11.4 | todo | |
| P13 | UI shell + level view *(parent: done when P13.1–P13.4 are)* | M4 | P10 | todo | |
| ↳ P13.1 | `ui/` scaffold (TypeScript) | M4 | P10.1 | todo | |
| ↳ P13.2 | Level view: layout + node cards | M4 | P13.1 | todo | |
| ↳ P13.3 | Edges, ports, entanglement | M4 | P13.2 | todo | |
| ↳ P13.4 | Navigation + live refresh | M4 | P13.2, P10.4 | todo | |
| P14 | UI node panel + editing *(parent: done when P14.1–P14.3 are)* | M4 | P13 | todo | |
| ↳ P14.1 | Panel + set status | M4 | P13.4, P10.3 | todo | |
| ↳ P14.2 | Body editor | M4 | P14.1 | todo | |
| ↳ P14.3 | Structure actions | M4 | P14.1 | todo | |
| P15 | UI runs + proposal review *(parent: done when P15.1–P15.4 are)* | M4 | P12, P14 | todo | |
| ↳ P15.1 | Transcript renderer | M4 | P13.1, P11.1 | todo | |
| ↳ P15.2 | Runs in the node panel | M4 | P15.1, P14.1, P11.4 | todo | |
| ↳ P15.3 | Proposal overlay | M4 | P13.3, P12.4 | todo | |
| ↳ P15.4 | Proposal editing + accept/reject | M4 | P15.3 | todo | |
| P16 | Frames index + history API *(parent: done when P16.1–P16.2 are)* | M5 | P10 | todo | |
| ↳ P16.1 | Frames table | M5 | P06.2, P10.4 | todo | |
| ↳ P16.2 | History API | M5 | P16.1, P10.2 | todo | |
| P17 | Playback scrubber + delivery overlay *(parent: done when P17.1–P17.2 are)* | M5 | P15, P16 | todo | |
| ↳ P17.1 | Scrubber | M5 | P16.2, P15.4 | todo | |
| ↳ P17.2 | Delivery overlay | M5 | P17.1, P05.1 | todo | |
| P18 | `apm-implementer` *(parent: done when P18.1–P18.2 are)* | M6 | P15 | todo | |
| ↳ P18.1 | Implementer agent + runner support | M6 | P12.4 | todo | |
| ↳ P18.2 | Implement in the UI | M6 | P18.1, P15.2 | todo | |
| P19 | `apm-reconciler` *(parent: done when P19.1–P19.2 are)* | M7 | P18 | todo | |
| ↳ P19.1 | `apm reconcile` | M7 | P17.2, P07.2 | todo | |
| ↳ P19.2 | Reconciler agent | M7 | P19.1, P11.4 | todo | |
| P20 | v1 release: package name, docs, publish | — | P19 | todo | |

**Parallel tracks:** after P03, P04 and P05 can proceed in parallel. After P10, three tracks can run side by side: the runner (P11 → P12), the UI (P13 → P14) and history (P16). They join again at P15 and P17.

A child phase can start as soon as *its own* dependencies are done, which is often before its parent's: for example, P05.1 (git wrapper) needs only P01b, and P05.2 (write lock) needs only P02.2. The child splits for P03–P19 are sized from P02's (roughly 150–300 lines each, tests included); a child that turns out bigger when built is split again (§12.1).

### 12.3 Phases

#### M1: core

**P00 — Phased implementation plan**
- Commit this document to the repo and add the phase breakdown.
- *Exit:* merged; the tracker exists.

**P01 — Workspace scaffold + CI** *(done in TypeScript, #2; replaced by P01b)*
- Delivered a pnpm/TypeScript workspace (`packages/core`, `packages/app`) with Biome, Vitest + fast-check, an `apm --version` binary, and a CI matrix on `ubuntu-latest` and `macos-latest`. The backend then moved to Python (§1), so P01b replaces all of it; the CI matrix carries over.
- *Exit:* met at the time (#2).

**P01b — Python backend scaffold + CI** · depends on P01
- A uv project at the repo root: `pyproject.toml` (`uv_build` backend, src layout, `requires-python >= 3.14`), `.python-version`, and a committed `uv.lock`. One distribution with an `apm` console script; `python -m apm` works too.
- Dev tools in the `dev` dependency group: ruff (lint and format, line length 100, PEP 8 naming), ty, pytest and Hypothesis.
- `apm --version` and `apm --help` only (Typer), with the version read from the installed package's metadata.
- GitHub Actions on PRs and `main`, on `ubuntu-latest` and `macos-latest`: `uv sync --locked`, `ruff format --check`, `ruff check`, `ty check`, `pytest`, `uv build`, and a smoke run of `apm --version` from the built wheel.
- Removes P01's TypeScript toolchain (`packages/`, pnpm, tsconfig, Biome, Vitest). `ui/` brings TypeScript back for the UI alone in P13.1.
- *Exit:* a fresh clone runs `uv sync && uv run pytest` green; CI is green on the PR; `uv run apm --version` prints the version.

**P02 — Node model, IDs and file I/O** (§3.2–3.4, §3.7)

Decomposed into five PRs (first attempt, #3, was ~1,150 lines of TypeScript). Stacked in the order below on P01b; the dependencies are the real ones.

- *Exit (checked when all children are done):* property test shows `parse(serialize(g)) ≡ g`, and that serialization is byte-stable (serializing twice gives identical bytes); golden-file tests pass; handle-ambiguity tests pass.

**P02.1 — Node model + IDs and handles** · depends on P01b
- Types: `Node` and `Delivery` (frozen dataclasses), the `Kind` and `Status` enums, plus the reserved `External` and `variant_of` fields.
- `new_id()` (UUIDv7 from the standard library's `uuid.uuid7()`, in creation order even within one ms), `handle()` (last 8 hex), `handles()` (lengthens only colliding handles, §3.7) and `resolve_handle()`, which raises `HandleError` with code `invalid`, `unknown` or `ambiguous`.
- *Exit:* ID ordering, ambiguity, collision and "never match the timestamp prefix" tests pass; a property test shows every displayed handle resolves back to its node.

**P02.2 — Plan-repo paths, atomic writes, bodies** · depends on P02.1
- Paths inside `.apm/`; `write_file_atomic` (temp file + `os.replace`); `read_file_if_exists`; `body_path` refuses anything but a node ID, so input can't escape `nodes/`.
- `nodes/<id>.md`: `render_body()` (Intent / Acceptance criteria / Notes, §3.3), `read_body` (missing → empty), `write_body`.
- *Exit:* round-trip and no-leftover-temp-file tests; path-escape tests; template snapshot tests.

**P02.3 — `apm.yaml` config** · depends on P02.2
- Schema (pydantic + PyYAML) with defaults, strict keys and types, and `line:col` YAML errors. Empty keys (YAML `null`) count as not set, so a commented-out block falls back to defaults. `default_repo()`.
- *Exit:* the §3.4 example parses; defaults, empty-key and each validation error are tested.

**P02.4 — `graph.ndjson` parser** · depends on P02.1
- The per-line pydantic schema and `parse_graph`: canonicalizes hand-written input (any order, CRLF, blank lines, byte-order mark, `null`s, missing `kind`/`status`, duplicate deps), and reports **every** bad line as `graph.ndjson:<line>: <field>: <problem>`. Whole-graph checks are P03.
- *Exit:* golden and hand-written fixtures parse to the same graph; each error case is reported with its line number.

**P02.5 — `graph.ndjson` serializer + graph file I/O** · depends on P02.2, P02.4
- `serialize_graph` / `serialize_node`: byte-stable (id order, fixed key order, nulls and empties omitted). Every line is checked against the parser's schema first, and a node the parser would reject raises `InvalidNodeError` instead of being written. `read_graph_file` / `write_graph_file`.
- *Exit:* the P02 exit criteria above, plus a property test that anything written reads back, and a test that a refused write leaves the existing file untouched.

**P03 — Invariants and computed status** (§2.3, §2.4)

Decomposed into four PRs. P03.2 → P03.3 are stacked; P03.4 needs only P03.1 and can go in parallel with them.

- *Exit (checked when all children are done):* one or more unit tests per invariant; a Hypothesis strategy for valid graphs; derived-status tests, including a dependency on a `dropped` node.

**P03.1 — In-memory `Graph` + test generator** · depends on P02
- `Graph` built from parsed nodes: by-id map, children index, `ancestors()`, `descendants()`, `is_leaf()`, `is_ancestor()`, computed `depth()`. Read-only; `mutate()` (P05) builds a new one per op.
- `graphs()`: a Hypothesis strategy in `tests/strategies.py` for **valid** graphs (tree + acyclic deps, delivery on leaves only), shared by later phases' property tests.
- *Exit:* index and walk tests on fixtures; a property test that every generated graph's walks agree with a naive recomputation.

**P03.2 — `validate()` + structural invariants (I1, I4, I7)** · depends on P03.1
- `validate(graph)` → `{ errors, warnings }`, each issue `{ code, message, nodes }`.
- I1 (one parent, parent exists, no containment cycle), I4 (every `depends_on` target exists), I7 (only leaves carry `delivery`).
- *Exit:* a failing fixture per check, naming the nodes involved; every generated graph validates clean.

**P03.3 — Dependency invariants (I2, I3) + I6 warning** · depends on P03.2
- I2: dependency cycle detection over all nodes, reporting one cycle path. I3: no dependency on an ancestor or descendant. I6 warning: a `done` parent with unfinished children.
- *Exit:* cycle, self-loop, ancestor and descendant fixtures; a property test that adding any back edge to a generated graph is caught by I2.

**P03.4 — Computed status** · depends on P03.1
- `blocked`, `ready`, `progress` (done leaves ÷ leaves that aren't dropped, whole subtree), and the dropped-dependency flag.
- *Decides:* open question 2 (parent status). The v1 default stays: parent status is stored, and I6 raises a warning.
- *Exit:* derived-status tests, including a dependency on a `dropped` node and a subtree that is entirely dropped.

**P04 — Lifting and level views** (§2.2, I5)

Decomposed into three PRs. P04.2 and P04.3 both build on P04.1 and can go in parallel.

- *Exit (checked when all children are done):* fixture tests cover deep cross-level edges, merged duplicates and ports; a property test confirms every underlying edge is accounted for **exactly once** at each level (drawn, internal or port).

**P04.1 — Lifted edges** · depends on P03.1
- `level_view(graph, root)` (`root=None` for the top level): the children of `root`, and each edge inside the subtree mapped to its child-of-`root` ancestors (§2.2): drawn (`a ≠ b`, merged with a count and the underlying edges) or internal (hidden).
- *Exit:* fixtures for sibling edges, deep cross-level edges, merged duplicates and internal edges.

**P04.2 — Boundary ports** · depends on P04.1
- Edges with one end outside `root` become incoming or outgoing ports, each naming the outside node and the inside child it attaches to.
- *Exit:* port fixtures (including the top level, which has none); the exactly-once property test over generated graphs.

**P04.3 — Entanglement (I5) + `check_all()`** · depends on P04.1, P03.3
- Cycle detection on each level's lifted edges, reporting the nodes in the cycle. `check_all()` runs `validate()` plus I5 at every level; it backs `apm check`.
- *Exit:* an entangled fixture is reported at the right level only; a clean generated graph with no lifted cycles reports none.

**P05 — `mutate()`, write lock, git commits, `init`** (§3.1, §3.5, §6.2)

Decomposed into six PRs. P05.1, P05.2 and P05.3 are independent of each other; P05.4 joins them, and P05.5 → P05.6 stack on it.

- *Exit (checked when all children are done; = M1):*
  - A property test over random op sequences shows I1–I4 hold after every committed op, and that a rejected op leaves HEAD and the working tree untouched.
  - A multi-process test with N processes calling `mutate()` concurrently produces N commits and loses no updates.
  - A stale lock is recovered.
  - Trailers round-trip through `git log`.

**P05.1 — Git wrapper** · depends on P01
- `subprocess`-based `add`, `commit` (subject, body and trailers per §3.5), `rev-parse`, `show`, `log` (with trailer parsing) and `is-ancestor`. Typed errors carrying git's stderr.
- A `temp_repo` pytest fixture, reused by every later phase that touches git.
- *Exit:* trailers written by `commit` round-trip through `log`; each command is tested against a real temp repo.

**P05.2 — Write lock** · depends on P02.2
- `.cache/write.lock` created with `O_EXCL`, holding PID, hostname and a timestamp. Stale if the PID is dead on this host. Acquisition retries with a timeout; release only removes a lock this process owns.
- *Exit:* contention and timeout tests; a stale lock (dead PID) is recovered; a multi-process test shows the lock is never held twice.

**P05.3 — `init_project()` + project discovery** · depends on P05.1, P02.3, P02.5
- `init_project()`: `.apm/` (`git init -b main`), `apm.yaml`, empty `graph.ndjson`, `.apm/.gitignore`, `.apm-link`, `.apm/` appended to the code repo's `.gitignore`, and the first commit. Refuses to run over an existing project.
- `find_project()`: `APM_PROJECT`, otherwise walk up from the cwd to `.apm-link`.
- *Exit:* init in a temp repo yields exactly the §3.1 layout and one commit; discovery tests from nested directories and via `APM_PROJECT`.

**P05.4 — `mutate()` core** · depends on P05.1, P05.2, P05.3, P03.3
- `mutate(op, fn)` follows the 5 steps in §6.2: lock, reload, apply `fn` to a draft, validate (I1–I4, I7), write `graph.ndjson` and bodies, commit with trailers, emit `graph.changed` in-process. A rejected op restores the files it wrote and makes no commit.
- *Exit:* a rejected op leaves HEAD and the working tree untouched; the N-process concurrency test produces N commits with no lost updates.

**P05.5 — Node ops: create, update, status, deliver, edit-body** · depends on P05.4
- Typed op functions on top of `mutate()`, each one commit with the §3.5 subject and trailers. `create` writes the body template.
- *Exit:* one test per op checking the graph, the body file and the commit message.

**P05.6 — Structure ops: move, delete, link, unlink + op property test** · depends on P05.5
- `move`, `delete` (removes incoming edges and the body file in the same op), `link`, `unlink`.
- *Decides:* whether deleting a node with children is refused or requires `--recursive` (proposed: refuse unless recursive; a recursive delete is one commit).
- *Exit:* one test per op; the random-op-sequence property test from the P05 exit criteria.

#### M2: CLI, MCP, dogfood

**P06 — SQLite index + FTS** (§4)

Decomposed into three PRs, stacked.

- *Exit (checked when all children are done):* deleting `.cache/` and then reading rebuilds an identical index; a history rewrite is detected; FTS covers titles and bodies.

**P06.1 — SQLite wrapper + migrations** · depends on P05.3
- A thin wrapper over the standard library's `sqlite3`; opening the database checks that FTS5 is available. WAL mode; the `meta` table; migrations keyed by `schema_version`.
- *Exit:* migrations apply once and in order; reopening an existing DB is a no-op.

**P06.2 — Graph tables + freshness** · depends on P06.1, P05.4
- `nodes`, `edges`, `closure` (including self at depth 0). `ensure_fresh()` compares HEAD with `last_indexed_sha`, indexes forward, and rebuilds when the old HEAD isn't an ancestor. `reindex()`. `mutate()` updates the index after each commit.
- `frames` lands in P16; `runs`, `run_events` and `proposals` land in P11–P12.
- *Exit:* deleting `.cache/` rebuilds an identical index; a rewrite (reset to an older commit plus a new commit) triggers a rebuild; closure matches `Graph.ancestors()` on generated graphs.

**P06.3 — FTS5 search** · depends on P06.2
- `nodes_fts` over title + body, kept current by the same indexing path; `search(query)` returning ranked node IDs.
- *Exit:* search finds words from titles and bodies, and a body edited after indexing is found by its new text only.

**P07 — `apm` CLI** (§10)

Every §10 command except `serve`, `mcp`, `run` and `proposal`, which arrive with their subsystems. Includes `apm commit` for hand edits (described in §10's prose but missing from its command list). Decomposed into five PRs: P07.1 first; P07.2 → P07.3 → P07.4 are stacked, and P07.5 can go in parallel with them.

- *Exit (checked when all children are done):* end-to-end tests drive the `apm` command through init → add → nest → link → status → check → commit → reindex in a temp repo.

**P07.1 — CLI framework, outline, `init`, `node ls`** · depends on P05.3, P03.4
- The Typer app: project discovery, handle resolution with readable ambiguity errors, non-zero exit codes on errors, and `--json` output on read commands.
- The shared outline formatter (`1c0b9a44 [work/todo·ready] Title → 7d2e0f11`), reused by the MCP in P08.
- `apm init` and `apm node ls [--root h] [--depth n]`.
- *Exit:* formatter snapshot tests; e2e tests of `init` and `node ls` against the `apm` command, including an ambiguous handle and `--json`.

**P07.2 — Node commands** · depends on P07.1, P05.6
- `node add`, `show`, `mv`, `rm`, `status`; `link-dep`, `unlink-dep`.
- *Exit:* e2e test of each command, checking the resulting commit; invariant violations exit non-zero with the invariant named.

**P07.3 — `node edit`, `check`, `commit`** · depends on P07.2, P04.3
- `node edit` via `$EDITOR` → `edit-body` commit (nothing if unchanged). `apm check` prints `check_all()` errors and warnings. `apm commit` validates hand edits in `.apm/` and commits them as `edit-body` or `update`.
- *Exit:* e2e with a scripted `$EDITOR`; `check` on a broken and an entangled fixture; `commit` refuses an invalid hand edit.

**P07.4 — `ready` + `reindex`** · depends on P07.3, P06.3
- `apm ready [--root h]` and `apm reindex`.
- *Exit:* the full e2e flow from the P07 exit criteria.

**P07.5 — Plan-repo remote: `link --remote`, `clone`, `push`, `pull`** · depends on P07.1
- Thin git wrappers; `link --remote` records the remote in `.apm-link`, and `clone` reads it on a fresh checkout.
- *Exit:* e2e against a bare temp repo as the remote: link → push → clone elsewhere → pull.

**P08 — `apm mcp` + plugin skeleton + skills** (§6.4, §8)

Decomposed into four PRs. P08.1 → P08.2 → P08.3 are stacked; P08.4 needs only P08.1.

- *Exit (checked when all children are done):* an in-process MCP client calls every tool; every scope has tests for both allowed and refused calls; `claude --plugin-dir ./plugin` lists the APM tools and skills.

**P08.1 — MCP server + read tools** · depends on P07.1, P06.3
- A stdio server on the official `mcp` Python SDK; `apm mcp`. `apm_get_graph`, `apm_get_node`, `apm_search`, `apm_ready`, with typed input schemas and outline-style output.
- *Exit:* an in-process client calls each read tool against a fixture project.

**P08.2 — MCP write tools** · depends on P08.1, P05.6
- `apm_create_nodes` (batch, one commit), `apm_update_node`, `apm_move_node`, `apm_link` / `apm_unlink`, `apm_set_status`, `apm_attach_delivery` (leaves only).
- *Exit:* each tool produces exactly one commit; invalid input returns a tool error naming the invariant.

**P08.3 — `APM_RUN_SCOPE` enforcement** · depends on P08.2
- `read`, `node:<id>` and `project` scopes, checked before any tool runs; an unknown scope refuses every write.
- *Exit:* allowed and refused calls tested for every scope and tool.

**P08.4 — Plugin skeleton + skills** · depends on P08.1
- `plugin/`: `.claude-plugin/plugin.json`, `.mcp.json`, and the `apm-node-format` and `apm-decompose` skills. The agent definitions land with their phases (P12, P18, P19).
- *Exit:* `claude --plugin-dir ./plugin` lists the APM tools and skills (checked by hand and recorded in the PR).

**P09 — Dogfood: plan APM in APM**

Stays one PR: it's mostly a data change. A fix found while dogfooding that won't fit goes in its own PR (`P09.1`, `P09.2`, …).

- Run `apm init` in this repo. The plan repo gets its own private remote (to be created by you). This PR commits only `.apm-link` and the `.gitignore` entry to the code repo.
- Enter the remaining phases (P10+) as nodes through an interactive `claude` session with the plugin, with milestones as the top-level nodes, phases as their children and child phases below those.
- Fix the problems found while dogfooding, and list them in the PR.
- *Exit (= M2):* the APM plan is maintained in APM; `apm ready` shows the next phase.

#### M3: server, runner, proposals

**P10 — `apm serve`: HTTP/WS core and security** (§6.3)

Decomposed into four PRs. P10.1 first; P10.2 → P10.3 stack on it, and P10.4 can go in parallel with them.

- *Exit (checked when all children are done):* the security tests reject a missing or wrong token, a foreign `Origin` and a non-loopback bind; a CLI mutation shows up as a WS event.

**P10.1 — Server core + security** · depends on P07.1, P04.3
- HTTP server (FastAPI on uvicorn) bound to `127.0.0.1`. A random token written to `.cache/server.json` and required on every request (header, or query parameter for the WS upgrade). `Origin` checked on every request. `apm serve [--port]` prints the tokenized URL.
- The first real endpoint, `GET /api/graph?root=` (level view with lifted edges, ports and warnings), so the server ships with something that works.
- *Exit:* the security tests from the P10 exit criteria; `GET /api/graph` matches `level_view()` for a fixture.

**P10.2 — Node read endpoint** · depends on P10.1
- `GET /api/nodes/:id`: node, body, computed status and the node's history (`git log` of its body and graph line). Runs are added in P11.
- *Exit:* fixture tests, including an unknown and an ambiguous handle.

**P10.3 — Write endpoints** · depends on P10.2, P05.6
- `POST /api/nodes`, `PATCH /api/nodes/:id`, `POST /api/nodes/:id/move`, `DELETE /api/nodes/:id`, `POST/DELETE /api/edges`. Invariant violations → 409 with the invariant named.
- *Exit:* each endpoint produces exactly one commit; each violation is a 409.

**P10.4 — WS `graph.changed`** · depends on P10.1
- `/ws` with token auth. `graph.changed` fires for in-process mutations and for commits made by *other* processes (the CLI or MCP), by watching `.apm` refs (`watchfiles`) with a polling fallback.
- *Exit:* a CLI mutation shows up as a WS event; the polling fallback is tested with the watcher disabled.

**P11 — Runner: spawning `claude`, run logs, cancel** (§7.1, §7.3)

Decomposed into four PRs, stacked.

- *Exit (checked when all children are done):* tests against a **fake `claude` script** that emits canned stream-json cover success, failure, cancel and the limits. A real run answers the three "to check at M3" questions in §7.1, and the answers are written back into §7.1.

**P11.1 — Run tables, stream-json parser, fake `claude`** · depends on P06.1
- `runs` and `run_events` tables (migration). A line parser for stream-json that keeps raw lines and extracts the final `result` event. The fake `claude` script and its canned outputs, used by every later runner test.
- *Exit:* parser tests over canned success, error and truncated streams.

**P11.2 — Spawn a run** · depends on P11.1, P08.3, P10.4
- The per-run MCP config (injecting `APM_PROJECT` and `APM_RUN_SCOPE`); the argument builder with exactly the §7.1 flags, model and budget from `apm.yaml`; spawn with `asyncio.create_subprocess_exec`, stream each line to `run_events` and WS `run.event`, then `run.finished`.
- *Exit:* argument-builder snapshot test; fake-`claude` success and failure runs end in the right `runs` state with every event stored.

**P11.3 — Limits, cancel, resume** · depends on P11.2
- `max_concurrent` in total and one active run per node (extra runs rejected, not queued). Cancel: SIGTERM, then SIGKILL after 5s. Resume via `--resume <session>`.
- *Exit:* fake-`claude` tests for both limits, cancel of a script that ignores SIGTERM, and resume arguments.

**P11.4 — Runs API + `apm run`** · depends on P11.3, P10.2
- `POST/DELETE /api/runs`; runs listed on `GET /api/nodes/:id`. `apm run <agent> <h> [--guidance]` calls the server and tails the stream in the terminal.
- A real run answers the three §7.1 questions; the answers are written into §7.1 in this PR.
- *Exit:* e2e of `apm run` against a server using the fake `claude`; the §7.1 answers are recorded.

**P12 — Decomposer, critic and proposals** (§7.2)

Decomposed into four PRs. P12.2 and P12.3 both build on P12.1 and can go in parallel; P12.4 joins them.

- *Exit (checked when all children are done; = M3):* fixture tests cover bad keys, cycles introduced by a proposal (rejected) and additive-only violations. A real run of `apm run apm-decomposer <h>` → `apm proposal accept` produces exactly one commit.

**P12.1 — Schemas + agent definitions** · depends on P08.4
- The decomposition JSON schema (§7.2) and the critic's output schema, mirrored as pydantic models. `plugin/agents/apm-decomposer.md` and `apm-critic.md`.
- *Decides:* open question 1 (the policy for near-duplicates flagged by the critic), written into the critic's instructions.
- *Exit:* schema tests on valid and invalid fixtures; the agents load with `claude --plugin-dir ./plugin`.

**P12.2 — Proposal pipeline** · depends on P12.1, P11.3
- The `proposals` table; decomposer → critic → at most one revision → a `proposals` row → WS `proposal.created`.
- *Exit:* fake-`claude` tests for a clean pass, one revision, a critic that still objects after the revision, and a failed run (no proposal).

**P12.3 — Accepting a proposal** · depends on P12.1, P05.6
- `accept_proposal(proposal, edits=None)` in core: assign UUIDv7s, resolve keys, enforce additive-only, write bodies from `intent` and `acceptance`, then one `decompose` commit with `APM-Actor` and `APM-Run` trailers.
- *Exit:* the bad-key, cycle and additive-only fixture tests from the P12 exit criteria; an accepted proposal is exactly one commit.

**P12.4 — Proposals API + CLI** · depends on P12.2, P12.3, P11.4
- `POST /api/proposals/:id/accept|reject` and `apm proposal ls|show|accept|reject`.
- *Exit:* e2e with the fake `claude`; then the real `apm run apm-decomposer` → `apm proposal accept` check from the P12 exit criteria.

#### M4: UI

**P13 — UI shell + level view** (§9)

Decomposed into four PRs. P13.1 → P13.2 first; P13.3 and P13.4 can then go in parallel. The view is read-only throughout.

- *Exit (checked when all children are done):* component tests for cards, badges and ports; a Playwright smoke test in CI loads a fixture project and drills down two levels.

**P13.1 — `ui/` scaffold (TypeScript)** · depends on P10.1
- `ui/`: the UI's own pnpm workspace (Vite + React + `@xyflow/react` + elkjs, Biome, Vitest). `apm serve` serves the built UI, which ships inside the wheel; in development, Vite proxies to the server. The token is read from the URL and kept in `sessionStorage`. A typed API client.
- CI gains a UI job (lint, typecheck, tests, build) and a Playwright job (pytest-playwright) that starts `apm serve` on a fixture project.
- *Exit:* the Playwright job loads the page and shows the project name.

**P13.2 — Level view: layout + node cards** · depends on P13.1
- `@xyflow/react` + elkjs layered layout of a level. Node cards: handle, title, stored and computed status, progress bar, has-children marker.
- *Exit:* card component tests; layout test on a fixture level.

**P13.3 — Edges, ports, entanglement** · depends on P13.2
- Lifted-edge count badges, boundary ports at the canvas edges, and the entanglement banner.
- *Exit:* component tests for badges, ports and the banner.

**P13.4 — Navigation + live refresh** · depends on P13.2, P10.4
- Double-click to drill down, breadcrumb back up, and live refresh on WS `graph.changed`.
- *Exit:* the Playwright drill-down test from the P13 exit criteria; a CLI mutation re-renders the open level.

**P14 — UI node panel + editing**

Decomposed into three PRs. P14.2 and P14.3 both build on P14.1 and can go in parallel.

- *Exit (checked when all children are done):* each action round-trips to exactly one commit; an edit made in the CLI appears live in the open panel.

**P14.1 — Panel + set status** · depends on P13.4, P10.3
- The node panel: metadata, delivery display, history, and set status.
- *Exit:* Playwright: open a node, set its status, see one new commit in its history.

**P14.2 — Body editor** · depends on P14.1
- Markdown body editor; saving makes one `edit-body` commit. An edit made in the CLI updates the open panel (with a conflict prompt if there are unsaved local changes).
- *Exit:* save round-trip; the live CLI-edit test.

**P14.3 — Structure actions** · depends on P14.1
- Dependencies (add and remove), add child, delete, move.
- *Exit:* each action round-trips to exactly one commit; a refused action shows the invariant.

**P15 — UI runs + proposal review**

Decomposed into four PRs. P15.1 → P15.2 (runs) and P15.3 → P15.4 (proposals) are two parallel stacks.

- *Exit (checked when all children are done; = M4):* a feature is planned entirely in the UI, from a new node → decompose → edit → accept → drill in → decompose again.

**P15.1 — Transcript renderer** · depends on P13.1, P11.1
- A component that renders stream-json events (assistant text, tool calls and results, the final result) incrementally.
- *Exit:* component tests over the fake `claude` canned streams.

**P15.2 — Runs in the node panel** · depends on P15.1, P14.1, P11.4
- Decompose action (with optional guidance), the node's run list, the live transcript over WS, cancel, and continue (resume).
- *Exit:* Playwright with the fake `claude`: start, watch, cancel, continue.

**P15.3 — Proposal overlay** · depends on P13.3, P12.4
- Ghost nodes and edges from a pending proposal, overlaid on the level view; opened from `proposal.created`.
- *Exit:* component tests on a fixture proposal.

**P15.4 — Proposal editing + accept/reject** · depends on P15.3
- Inline editing of title, kind, intent and acceptance; removal of children or edges; Accept or Reject.
- *Exit:* Playwright: edit, remove a child, accept → one commit; the M4 walkthrough from the P15 exit criteria.

#### M5: playback

**P16 — Frames index + history API** (§5)

Decomposed into two PRs, stacked.

- *Exit (checked when all children are done):* the frame count equals the number of plan commits; `?at=` returns the historical level view; after a rewrite, the frames are rebuilt.

**P16.1 — Frames table** · depends on P06.2, P10.4
- `frames` built in one `git log --reverse` pass with trailers; incremental via `last_indexed_sha`; a history rewrite triggers a rebuild. WS `index.progress` on large rebuilds.
- *Exit:* frame count equals commit count; incremental and rewrite tests.

**P16.2 — History API** · depends on P16.1, P10.2
- A lazy blob loader with an LRU of parsed graphs; `GET /api/frames`; `GET /api/graph?at=<sha>`; per-node body history.
- *Exit:* `?at=` returns the historical level view; LRU eviction test.

**P17 — Playback scrubber + delivery overlay**

Decomposed into two PRs, stacked.

- *Exit (checked when all children are done; = M5):* the APM plan (from P09) replays from its first commit.

**P17.1 — Scrubber** · depends on P16.2, P15.4
- Timeline scrubber (step, play, labels showing each frame's op and actor). Scrubbing puts the view in read-only mode.
- *Exit:* Playwright: scrub back, edits are disabled, return to live.

**P17.2 — Delivery overlay** · depends on P17.1, P05.1
- Code-repo scan for `APM-Node` trailers (in core, reused by P19), merged into the timeline by timestamp and shown as markers on nodes.
- *Exit:* scan tests on a temp code repo; the P17 replay check.

#### M6–M7: delivery

**P18 — `apm-implementer`** (§7.4)

Decomposed into two PRs, stacked.

- *Exit (checked when all children are done; = M6):* a ready leaf becomes a PR whose commits carry `APM-Node` and whose node shows the delivery.

**P18.1 — Implementer agent + runner support** · depends on P12.4
- The `apm-implementer` agent and the `apm-deliver` skill (branch naming, the `APM-Node` trailer, a PR body template; PR via `gh` when available, then `apm_attach_delivery` and `in_progress`).
- The runner starts it in a worktree (`-w apm/<handle>-<slug>`) with scope `node:<id>` and the `allowed_tools` from `apm.yaml`. The **server** refuses to start it on a node that isn't a ready leaf.
- *Exit:* argument-builder and refusal tests; a real run on a toy ready leaf yields a PR (recorded in the PR).

**P18.2 — Implement in the UI** · depends on P18.1, P15.2
- An Implement button on ready leaves, and the delivery (branch, commits, PR) shown on the node.
- *Exit:* Playwright with the fake `claude`; the P18 exit check.

**P19 — `apm-reconciler`**

Decomposed into two PRs, stacked.

- *Decides:* how much of this is deterministic code (a trailer scan plus `gh pr view`) and how much is left to the agent. Recommendation: do the deterministic work in an `apm reconcile` command, and keep the agent for ambiguous cases. The split below assumes the recommendation.
- *Exit (checked when all children are done; = M7):* merging a PR moves its node to `done` with no manual step.

**P19.1 — `apm reconcile`** · depends on P17.2, P07.2
- Trailer scan plus `gh pr view` → status updates, as one commit with `APM-Actor: reconciler`. Runs manually, and once when `apm serve` starts.
- *Exit:* tests with a stubbed `gh`: merged → `done`, open → unchanged, closed unmerged → reported, not changed.

**P19.2 — Reconciler agent** · depends on P19.1, P11.4
- The `apm-reconciler` agent (scope `project`), started for the cases `apm reconcile` reports as ambiguous.
- *Exit:* a real merge moves its node to `done` with no manual step.

**P20 — v1 release**

Stays one PR: mostly docs and a workflow, which don't count toward the size limit.

- *Decides:* open question 4 (the package name) and open question 3 (whether to auto-commit hand edits).
- README, install instructions for the plugin, `uvx` / `uv tool install`, and a PyPI publish workflow (trusted publishing).
- *Exit:* `uvx <pkg> init` works on a clean machine.

---

## 13. Deferred (v2+)

- Competing breakdowns: `--fork-session` + `variant_of`, compared side by side
- GitHub Issues / Linear / Jira sync (the `external` field is already reserved)
- Multiple code repos per project; multiple users; automatic push
- A researcher/spike agent
- Keeping the plan on an orphan branch instead of a separate repo (`plan.location` config)
- "Restore from here" in playback
- Non-destructive decomposition that can also restructure existing children

---

## 14. Open questions

Each question is resolved in the phase noted in §12.3 (Q1 → P12, Q2 → P03, Q3 and Q4 → P20).


1. **Duplicates across parents.** Because subtasks can't be shared, how should a near-duplicate be handled? v1: the critic flags it and you decide. Candidate policy: move it up to the lowest common ancestor.
2. **Parent status.** v1 stores it and warns on I6. Should it instead be fully derived from the children once a node has any?
3. **Auto-committing hand edits.** v1 requires an explicit `apm commit`. Is a server-side file watcher with a debounce worth it?
4. **Package name.** `apm` is taken on both PyPI and npm (Atom's old package manager). Choose a new distribution name before publishing; the command can stay `apm`.
