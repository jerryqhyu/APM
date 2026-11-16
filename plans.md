# APM — Design Plan

**Status:** v6 draft, scoped for v1 · design only: the phase breakdown was abandoned and will be redone from this plan · **Last updated:** 2026-11-16

APM is a local-first project manager built for working with AI agents. A project is a **nested graph of nodes**: you start with a big idea, then break it down with Claude agents, level by level, until each piece is small enough to ship as a commit or PR. The plan lives in its own git repo, so every change to it can be traced and **played back** from the project's first node onward.

v1 = a Python backend with a TypeScript web UI, one user on one machine, one code repo, Claude CLI integration.

---

## 1. Decisions

| Topic | Decision | Why |
|---|---|---|
| Language | **Python 3.14** for the backend (core, CLI, server, MCP), managed with **uv**; TypeScript only for the web UI | The standard library covers UUIDv7, SQLite with FTS5 and subprocess git; first-class MCP and Claude Agent SDKs; fast, strict tooling (uv, ruff, ty). The UI stays TypeScript because it runs in the browser. Installed with `uvx` / `uv tool install`. Replaces TypeScript-everywhere, before any core code merged |
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
- **Python style:** type hints on everything `ty` checks, `pathlib` for paths, frozen dataclasses for value types, and pydantic only at input boundaries (files, API, MCP).
- **Tests mirror the code:** tests under `tests/` mirror `src/apm/` (`tests/core/test_ids.py` tests `apm/core/ids.py`); shared Hypothesis strategies live in `tests/strategies.py`. End-to-end tests run the `apm` command against throwaway git repos in a temp dir.
- **No dead surface:** a command, endpoint or tool is registered only once it works. Nothing ships as a "not implemented" stub.
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

Milestones are the roadmap. The breakdown into PR-sized pieces is still to be planned.

---

## 12. Deferred (v2+)

- Competing breakdowns: `--fork-session` + `variant_of`, compared side by side
- GitHub Issues / Linear / Jira sync (the `external` field is already reserved)
- Multiple code repos per project; multiple users; automatic push
- A researcher/spike agent
- Keeping the plan on an orphan branch instead of a separate repo (`plan.location` config)
- "Restore from here" in playback
- Non-destructive decomposition that can also restructure existing children

---

## 13. Open questions

1. **Duplicates across parents.** Because subtasks can't be shared, how should a near-duplicate be handled? v1: the critic flags it and you decide. Candidate policy: move it up to the lowest common ancestor.
2. **Parent status.** v1 stores it and warns on I6. Should it instead be fully derived from the children once a node has any?
3. **Auto-committing hand edits.** v1 requires an explicit `apm commit`. Is a server-side file watcher with a debounce worth it?
4. **Package name.** `apm` is taken on both PyPI and npm (Atom's old package manager). Choose a new distribution name before publishing; the command can stay `apm`.
5. **Deleting a node with children.** Refuse it, or require `--recursive`? Proposed: refuse unless recursive; a recursive delete is one commit.
6. **Reconciler split.** How much of reconciling is deterministic code (a trailer scan plus `gh pr view`) and how much is left to the agent? Recommendation: do the deterministic work in an `apm reconcile` command, and keep the agent for ambiguous cases.
