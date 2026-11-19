# APM — Design Plan

**Status:** v9 · v1 is implemented except playback (M5) and the SQLite index; §11 has the state of each milestone · **Last updated:** 2026-11-19

APM is a local-first project manager built for working with AI agents. A project is a **nested graph of nodes**: you start with a big idea, then break it down with Claude agents, level by level, until each piece is small enough to ship as a commit or PR. The plan lives in its own git repo, so every change to it can be traced and **played back** from the project's first node onward.

v1 = a Python backend with a TypeScript web UI, one user on one machine, one code repo, Claude CLI integration.

---

## 1. Decisions

| Topic | Decision | Why |
|---|---|---|
| Language | **Python 3.14** for the backend (core, CLI, server, MCP), managed with **uv**; TypeScript only for the web UI | The standard library covers UUIDv7, SQLite with FTS5 and subprocess git; first-class MCP and Claude Agent SDKs; fast, strict tooling (uv, ruff, ty). The UI stays TypeScript because it runs in the browser. Installed with `uvx` / `uv tool install`. Replaces TypeScript-everywhere, before any core code merged |
| Graph model | Compound graph: containment **tree** + dependency **DAG**; cross-level edges stored once and *lifted* for display | Unlimited depth, zoom into any node, no hand-copying edges between levels |
| Graph library | **networkx**: containment and dependencies are two digraphs over the same node ids | Traversal, cycle detection and strongly connected components come from a well-tested library instead of hand-written walks |
| Plan storage | Separate git repo at `.apm/`, on your machine only, ignored by the code repo and found through `.apm-link` | No CI runs on plan commits; works with a protected `main`; history holds only plan changes |
| Source of truth | `graph.ndjson` (structure + metadata) + `nodes/<id>.md` (prose only) | Each field lives in exactly one place, so the two can't drift |
| IDs | UUIDv7; the short handle is the **last** 8 hex chars | Sorts by creation time; new nodes append at the end of the file, so diffs stay small |
| Local DB | SQLite (the standard library's `sqlite3`) in `.apm/.cache/`, not committed | Run logs and proposals. Can be deleted at any time |
| History | Git commits with structured trailers, indexed into SQLite | Git records the state and trailers record the intent, with no custom event log to maintain |
| Writes | `core.mutate()` under a file lock, callable from any process | No daemon required; writes happen one at a time, so updates aren't lost |
| Agents | Server spawns `claude` CLI; agents call back through the APM MCP | The server controls runs; the MCP is how agents read and write data |
| Planning writes | Two ways, both yours to choose. **Decompose** returns a **proposal** (`--json-schema`); nothing is committed until you accept. The **chat** changes the plan directly, as you ask it to | A proposal keeps rejected ideas out of the history. The chat is a conversation you are in: each change is a commit with the agent's name on it, so it is visible and reversible |
| Asking the user | Agents run with Claude Code's two-way stream: permission prompts and questions come to the host and wait for an answer | A run can stop and ask instead of guessing or being refused; the permission mode is yours to pick per run (default `auto`) |
| Reconciling | Plain code (`apm reconcile`), not an agent | "Did it merge?" has a definite answer: the PR's state, or whether the commits are on the default branch |
| Packaging | Agents + skills + MCP ship as one Claude Code plugin | The same plugin serves spawned runs and your own terminal sessions |
| Sharing a project | One `.apm.zip` archive (`apm export` / `apm import`, §3.8): the plan files plus a git bundle of the plan repo; runs only if asked | A whole project, history included, moves as one file with no remote to set up. Machine-specific state stays behind, and runs stay out by default because transcripts can hold secrets |
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

- **blocked**: `todo` and at least one dependency is not `done`. Dependencies are inherited: a node also waits on whatever its ancestors wait on, so the children of a blocked parent are blocked too.
- **ready**: `todo`, not blocked, and not under a `dropped` ancestor.
- **progress** (parents): done leaves ÷ leaves that aren't dropped, across the whole subtree. A leaf under a `dropped` ancestor counts as dropped.
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
  .apm-link                 in the CODE repo: { "path": ".apm", "remote": null }
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

Ops: `create, update, move, delete, link, unlink, status, deliver, decompose, edit-body, merge`. The plan repo's first commit is `init <name>`, with `APM-Op: init`.

`merge` folds several nodes into one: the target keeps its id, status and kind, and takes on the others' children, dependencies, dependents and bodies (appended under a heading). Edges among the merged nodes disappear. At most one of them may carry a delivery.

Commits are made through the `git` CLI (`subprocess` with an argument list, never a shell), so your identity, signing, and hooks apply.

### 3.6 Code-repo trailer

Every delivery commit in the code repo carries `APM-Node: <full-uuid>`. This is how code links back to the plan, and it's what the reconciler reads.

### 3.7 IDs and handles

- Stored IDs are full UUIDv7 values.
- The **handle** is the last 8 hex chars (`1c0b9a44`). All CLI and MCP inputs accept an unambiguous handle suffix of at least 4 hex chars, or a full ID (with or without hyphens, any case).
- Those 8 chars are 32 random bits, so two nodes can share them (about a 1% chance somewhere in a 10,000-node project). Where that happens, both are displayed with as many extra chars as it takes to tell them apart.
- ⚠ Do **not** use the leading characters. In UUIDv7 those are the timestamp, so nodes created within about a minute of each other share their first 8 characters.

### 3.8 Archives (`.apm.zip`)

`apm export` (and `GET /api/export`) write the whole project as one zip file. Format version 1:

```
manifest.json        what wrote it and what is in it (below)
apm.yaml             the plan files, exactly as committed at HEAD
graph.ndjson
nodes/<uuid>.md
plan.bundle          git bundle of every ref in the plan repo: the full history
runs.ndjson          with --with-runs only: one run per line, with all its events
proposals.ndjson     with --with-runs only: one proposal per line
```

```json
{"format": "apm-archive", "format_version": 1, "apm_version": "…", "exported_at": "…",
 "project": "my-app", "store_schema_version": 2,
 "counts": {"nodes": 42, "commits": 310, "runs": 12, "events": 4096, "proposals": 3}}
```

`store_schema_version` and the run counts are present only when runs are included; their presence is what says so.

Rules:

- **Committed state only.** Plan files come from HEAD, not the working tree, so they agree with the bundle; uncommitted hand edits are left out. Export holds the write lock throughout and writes to a temporary file renamed into place, so a failed export leaves nothing behind.
- **What stays behind:** the code repo's `.apm-link` (and so any remote), and everything under `.apm/.cache/`: the server token, the write lock, the database file itself.
- **Runs are opt-in** (`--with-runs`), and both the CLI and the UI warn that transcripts can hold tool output and secrets.
- **Import is all or nothing.** It refuses a folder that already has `.apm/` or `.apm-link`; a `format_version` or `store_schema_version` newer than this APM knows; and a damaged file (not a zip, truncated, missing entries, counts that don't match, plan files that differ from the bundle's HEAD). It builds the plan repo and database in a temporary folder that becomes `.apm/` only when complete, then links it as `apm init` does. It reads only the file and never contacts a remote.
- **Imported runs are history.** They keep their ids, times and sequence numbers; runs from an older store schema are filled in with defaults. A run that was `running` at export is stored as `cancelled` (shown as *Stopped*), and nothing is started or resumed.

---

## 4. Local database (SQLite)

`.apm/.cache/apm.db`, using the standard library's `sqlite3`. WAL mode. It holds only what matters on this machine:

```sql
meta(key PK, value)                           -- schema_version
runs(id PK, node_id, agent, session_id, status, started, ended, cost_usd, error,
     prompt, parent_run, pid)
run_events(run_id, seq, ts, json)             -- the stream-json events a transcript needs
proposals(id PK, node_id, run_id, json, review, status, created, resolved)
```

Deleting `.cache/` loses run logs and pending proposals, never anything about the project itself. They leave the machine only in an archive made with `--with-runs` (§3.8).

**No index of the plan yet.** Reads parse `graph.ndjson` into the in-memory graph each time, and search scans titles and bodies directly. At the size of a real plan (hundreds of nodes) that is instant, and there is nothing to keep in sync. The tables below are the design for when it stops being enough, and for playback (§5):

```sql
nodes(id PK, parent, title, kind, status, depth)
edges(src, dst, PRIMARY KEY(src, dst))
closure(ancestor, descendant, depth)          -- containment, incl. self at depth 0
nodes_fts                                     -- FTS5 over title + body
frames(seq PK, sha, ts, op, actor, node_ids)  -- one row per plan commit
```

---

## 5. History and playback

**Not built yet.** A node's own history (the plan commits that touched it) is shown in the UI; the timeline below is the design for M5.

Build the frames in one pass:

```bash
git -C .apm log --reverse --format='%H%x00%ct%x00%(trailers:key=APM-Op,valueonly)%x00…' -- graph.ndjson
git -C .apm show <sha>:graph.ndjson        # one blob per frame
```

- Frames go into `frames`. The scrubber works entirely from SQLite and loads blobs lazily, holding a small LRU of parsed graphs.
- Body edits are shown in the node's history (`git log -- nodes/<id>.md`); they don't create new frames.
- **Delivery overlay:** read the code repo's log for commits with an `APM-Node` trailer, merge them into the timeline by timestamp, and show each one as a marker on its node.
- Playback is read-only. v2 may add "restore from here", which would add a new commit rather than rewrite history.

---

## 6. Components

```
                ┌──────────────────── apm.core ──────────────────────┐
                │ graph model · lifting · invariants · mutate()      │
                │ git I/O · SQLite (runs, proposals)                 │
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
src/apm/core/    the plan: model, files, graph, rules, level views, mutate, ops, store
src/apm/cli.py   the `apm` command (Typer)
src/apm/mcp/     stdio MCP server on the official `mcp` SDK
src/apm/runner/  starting `claude`: agent specs, the command line, questions, run logs
src/apm/server/  FastAPI app (HTTP + WebSocket), served by uvicorn; `static/` is the built UI
src/apm/plugin/  Claude Code plugin (agents, skills, .mcp.json): ships inside the wheel
tests/           pytest + Hypothesis, mirroring src/apm/
ui/              TypeScript: vite + react + @xyflow/react + elkjs; `pnpm build` writes into
                 src/apm/server/static/, so the wheel carries the UI
docs/            the user guide
```

Inside `core`, each module is one concern, and nothing is computed in two places:

| Module | Concern |
|---|---|
| `model`, `ids` | the node model; UUIDv7 ids and handles |
| `errors` | `ApmError`, the base of every error the user can cause. The CLI prints it as one line, the MCP returns it as the tool's error, the API maps it to a status |
| `ndjson`, `files`, `config` | `graph.ndjson`, node bodies, `apm.yaml` |
| `git`, `lock` | the git wrapper; the cross-process write lock |
| `graph` | the in-memory graph on networkx, with everything derived from it: handles, blocked, ready, progress |
| `validate`, `lifting` | invariants I1–I7; level views |
| `project` | `init_project()`, finding a project |
| `mutate`, `ops` | the one way to write; the operations built on it; committing hand edits |
| `views` | the one way to read: outline text, JSON views, search, a node's history |
| `proposals`, `reconcile`, `store` | proposals; marking merged work done; the local database |

- **Backend:** Python 3.14, one distribution with an `apm` console script, managed with **uv** (committed `uv.lock`, `uv_build` backend, src layout). **ruff** lints and formats, **ty** type-checks, **pytest** + **Hypothesis** test. Names follow PEP 8: snake_case for modules, functions, variables and fields, PascalCase for classes; ruff's pep8-naming rules enforce it.
- **Libraries:** networkx (the in-memory graph: `Graph.tree` and `Graph.deps` are `networkx.DiGraph`s), pydantic (validating `graph.ndjson` lines, `apm.yaml`, and API and MCP inputs), PyYAML, Typer, FastAPI + uvicorn, and the `mcp` SDK. The standard library covers UUIDv7 (`uuid.uuid7()`), SQLite with FTS5 (`sqlite3`) and git (`subprocess`).
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

- **All or nothing.** A change that breaks an invariant is refused before anything is written. If the commit itself fails (a hook refuses, say), the files and the git index are put back as they were.
- **Hand edits are never swept up.** `mutate()` stages only the files it writes. If one of those has uncommitted changes, it refuses and asks you to commit them first (`apm commit`, §10).
- **An operation that changes nothing makes no commit.**
- **`graph.changed`** is announced by the server, which watches the plan repo's HEAD. That way it fires for every commit, whoever made it: the UI, the CLI, or an agent through the MCP.

### 6.3 Server

The server can start agents that run tools, so a web page from any other site must not be able to reach it. Three checks, all of which must pass:

- It binds to **127.0.0.1 only**, and refuses a request whose `Host` isn't a loopback name. That stops DNS rebinding.
- If the browser sends an `Origin`, it must be this server.
- Every `/api` request carries a random token (`Authorization: Bearer …`), and so does the WebSocket (`?token=`). The UI gets it from the URL `apm serve` prints. The token is kept in `.cache/server.json` between runs, so a bookmarked URL keeps working.

```
GET    /api/project                         name, agents, permission modes, counts
GET    /api/tree                            every node with its computed state; errors, warnings
GET    /api/graph?root=<id>                 one level: nodes, lifted edges, ports, entanglement
GET    /api/nodes/:id                       node + body + links + history + proposals
GET    /api/ready?root= · /api/search?q=
GET    /api/export?with_runs=           the project as an .apm.zip download (§3.8)
POST   /api/nodes · PATCH /api/nodes/:id · POST /api/nodes/:id/move · DELETE /api/nodes/:id
POST   /api/merge           { target, others[], title? }
POST   /api/edges · DELETE /api/edges
POST   /api/reconcile · POST /api/commit    mark merged work done · commit hand edits
GET    /api/runs?node_id=&scope=project&status= · GET /api/runs/:id?after=<seq>
POST   /api/runs            { agent, node_id?, message?, critic?, mode? }   no node_id = the project
POST   /api/runs/:id/continue   { message, mode? }    the next turn of a conversation
DELETE /api/runs/:id        stop
GET    /api/requests        what running agents are waiting on you for
POST   /api/requests/:id    { allow, message?, answers? }
GET    /api/proposals?status= · GET /api/proposals/:id
POST   /api/proposals/:id/accept   { children? }   the edited or trimmed list, if you changed it
POST   /api/proposals/:id/reject
WS     /ws   graph.changed · run.started · run.event · run.finished
             · run.request · run.request_resolved · proposal.created · proposal.resolved
```

Node ids in the API can be full ids or handles.

### 6.4 MCP (`apm mcp`, stdio)

Calls core directly (no HTTP), so it works even when the server isn't running. Every tool takes handles and returns short text, not JSON.

| Tool | Notes |
|---|---|
| `apm_get_graph(root?, depth?)` | The **compact outline**: `1c0b9a44 [work/todo·ready] Stream agent output → 7d2e0f11` |
| `apm_get_node(node?)` | State, links, delivery and body. Without `node`: the node the run was started on |
| `apm_search(query)` | Titles and bodies |
| `apm_ready(root?)` | Ready leaves: "what can be worked on now" |
| `apm_check()` | Invariant errors and warnings |
| `apm_create_nodes(nodes[])` | Batch; one commit. Nodes in the batch can depend on each other by `key`, and nest under each other with `parent_key`, so a whole subtree is one call |
| `apm_update_node(node, title?, kind?, body?)` | |
| `apm_move_node(node, parent?)` | |
| `apm_link / apm_unlink(node, depends_on)` | |
| `apm_set_status(node, status, note?)` | |
| `apm_attach_delivery(node, branch?, commits?, pr?)` | Leaves only |
| `apm_delete_node(node, recursive?)` | |
| `apm_merge_nodes(target, others[], title?)` | |

**Finding the project:** use `APM_PROJECT` if it's set; otherwise walk up from cwd looking for `.apm-link`. (Implementer worktrees don't contain `.apm/`, so spawned runs always set `APM_PROJECT`.)

**Scoping (`APM_RUN_SCOPE`):**
- `read`: all write tools are refused. Used by the decomposer and critic.
- `node:<id>`: only `set_status` / `attach_delivery` / `update_node(body)` on that node. Used by the implementer.
- `project` (the default): no restriction. Used by the chat, and by your own interactive sessions.

**Attribution:** `APM_ACTOR` and `APM_RUN` become the `APM-Actor` and `APM-Run` trailers of every commit the session makes. `APM_NODE` names the node a run is about.

---

## 7. Claude CLI integration

### 7.1 Runner

The runner is a library (`apm.runner`). `apm serve` hosts one for the UI, and `apm run` hosts its own, so the CLI works without the server.

A **run** is one turn: one `claude` process, from your message to its reply. A **conversation** is a chain of runs that share a session, each resuming the one before (`parent_run`). Each run:

1. Inserts a `runs` row (with its permission mode) and generates a session UUID.
2. Writes a per-run MCP config (`.cache/runs/<id>.mcp.json`) with `APM_PROJECT`, `APM_RUN_SCOPE`, `APM_ACTOR`, `APM_RUN` and `APM_NODE`.
3. Spawns, from the code repo:

```bash
claude -p \
  --input-format stream-json --output-format stream-json --verbose \
  --permission-prompt-tool stdio --permission-mode auto \
  --agent apm:apm-assistant \
  --plugin-dir "$APM_PLUGIN" \
  --mcp-config "$RUN_MCP_JSON" --strict-mcp-config \
  --setting-sources project \
  --allowedTools "Read,Grep,Glob,mcp__apm__apm_get_graph,…" \
  --max-budget-usd 2 \
  --session-id "$SESSION_UUID"          # or --resume, for the next turn
```

4. Sends your message on stdin as a stream-json `user` message, and keeps stdin open.
5. Reads stdout line by line. The events a transcript needs (`system` init, `assistant`, `user`, `result`) go to `run_events` and out as `run.event`; the rest is dropped.
6. When the `result` event arrives, closes stdin so the process exits, and reads the cost, session id and structured answer from it.

**Questions and permission prompts.** With `--permission-prompt-tool stdio`, `claude` sends its host a `control_request` (`can_use_tool`) whenever something needs your say-so, and waits. The runner records it as a request, announces it (`run.request`), and waits too. `answer()` writes the `control_response`: `allow` (for `AskUserQuestion`, with your answers added to the tool's input) or `deny` with your reason. So a run can ask which option you want, or whether it may run a command, and carry on with the answer. In the terminal, `apm run` puts the same requests to you as prompts.

**Permission modes** are Claude Code's own, chosen per run: `auto` (the default: Claude decides what is safe and asks about the rest), `manual` (ask before every change), `acceptEdits`, `plan`, `dontAsk`. `runner.permission_mode` in `apm.yaml` sets the default. The critic always runs in `dontAsk`, so a review never stops to ask.

Limits: `max_concurrent` pipelines in total. One conversation takes one turn at a time; a node has one decomposer and one implementer at a time. A request beyond that is refused (HTTP 409), not queued. Stop = SIGTERM to the process group, then SIGKILL after 5s. A run still marked running whose host process has died is marked failed the next time a runner starts.

**Why each flag is there:**
- The MCP is passed with `--mcp-config` rather than through the plugin's `.mcp.json`, so each run can inject its own env vars. `--strict-mcp-config` keeps your personal MCP servers out of runs, and also drops the plugin's own MCP entry (checked).
- `--input-format stream-json` with `--permission-prompt-tool stdio` is what makes a run two-way (checked: prompts and `AskUserQuestion` arrive as `can_use_tool` requests).
- `--allowedTools` is what never needs asking: reading the code, and the plan's own tools. Everything else follows the permission mode.
- `--agent` needs the plugin's namespace: `apm:apm-decomposer` (checked).
- `--json-schema` output arrives in the `result` event as `structured_output` (checked).
- No `--include-partial-messages`: whole messages arrive block by block, which is live enough for a transcript and far fewer events to store.

**What each agent may use** (`apm.runner.agents`), on top of which `apm.yaml` can add `allowed_tools`, a `model` and a `budget_usd` per agent:

| Agent | Plan scope | Never asks for | Extra |
|---|---|---|---|
| `apm-assistant` (the chat) | `project` | Read, Grep, Glob, every plan tool | can be about one node or the whole project |
| `apm-decomposer` | `read` | Read, Grep, Glob, the read tools | returns a decomposition (JSON schema); file-editing tools are disallowed |
| `apm-critic` | `read` | Read, Grep, Glob, the read tools | returns a review (JSON schema); only started by the pipeline; `dontAsk` |
| `apm-implementer` | `node:<id>` | Read, Grep, Glob, Edit, Write, `Bash(git *)`, `Bash(gh pr *)`, its node's tools | `--worktree` |

Give the implementer the commands that run your tests, e.g. `allowed_tools: ["Bash(uv run *)"]` under `runner.agents.apm-implementer`, or it has to ask (or, in `dontAsk`, cannot run them).

### 7.2 Proposals (Decompose never commits by itself)

```
decomposer ──json──▶ critic ──revise?──▶ decomposer (1 revision max) ──▶ proposal
                                                                          │
                                   UI shows ghost children ◀──────────────┘
                                   you edit / trim / accept ──▶ mutate(decompose) → 1 commit
```

Each step is its own run, linked by `parent_run`. The critic returns `{ verdict: accept | revise, summary, issues[] }`. On `revise`, the decomposer's own session is resumed once with the review. A critic that fails doesn't sink the proposal: it is stored unreviewed. The critic can be skipped per run.

Decomposition schema: new nodes don't have IDs yet, so they use temporary keys.

```json
{
  "rationale": "string",
  "children": [
    { "key": "a", "title": "string", "kind": "work|decision",
      "intent": "string", "acceptance": ["string"],
      "depends_on_keys": ["b"], "depends_on_ids": ["<handle or id of an existing node>"] }
  ],
  "warnings": ["string"]
}
```

When a proposal is accepted, the children get UUIDv7s, the keys are resolved, the invariants are checked, and one `decompose` commit is made, with the agent as `APM-Actor` and the run as `APM-Run`. You can accept an edited or trimmed list. A proposal is claimed before it is applied, so it can't be accepted twice; if applying fails, it goes back to pending.

v1: decomposing a node that already has children is **additive only**. It can add children and edges, and it cannot modify or remove existing ones.

### 7.3 Conversations

`runs.session_id` records the Claude session of every run. Sending the next message of a conversation starts a new run that resumes the session with `--resume <id>`, from the directory the session started in (an implementer's is its worktree). The run keeps the conversation's permission mode unless you pick another. `--fork-session` is reserved for v2 variants.

The chat (`apm-assistant`) is the open-ended one: it has `project` scope, so within a conversation it can add, edit, link, move, merge and delete nodes as you ask. It can be started on a node (the conversation is then about that node) or on the project as a whole (`runs.node_id` is empty).

### 7.4 Implementer

- **Only offered for a leaf that can start:** ready (every dependency done), or already in progress.
- Spawned with `--worktree apm-<handle>-<slug>`, so it works in its own worktree under `.claude/worktrees/` and your checkout is never touched. `apm init` ignores that folder.
- Scope `node:<id>`.
- It works on branch `apm/<handle>-<slug>`. Commits carry `APM-Node:`. It opens a PR if `gh` is available.
- It calls `apm_set_status(in_progress)` when it starts and `apm_attach_delivery` when the work is pushed, and ticks the acceptance criteria it verified in the node's body.

### 7.5 Reconciling

`apm reconcile` marks an in-progress node `done` when its work has merged. It also runs once when `apm serve` starts. It is plain code:

- A node with a pull request is merged when `gh pr view` says the PR is. That covers squash merges.
- Otherwise it is merged when every delivery commit is on the code repo's default branch.
- If neither can be determined (no `gh`, offline, a commit this clone doesn't have), the node is left alone.

---

## 8. Agents and skills (plugin)

```
src/apm/plugin/
  .claude-plugin/plugin.json
  agents/
    apm-assistant.md      the chat: discusses the plan and changes it as asked
    apm-decomposer.md     node → proposal (read-only scope, json-schema)
    apm-critic.md         proposal → review: gaps, overlap, untestable criteria,
                          near-duplicates elsewhere in the graph, bad dependencies
    apm-implementer.md    ready leaf → worktree → commits → PR → attach delivery
  skills/
    apm-node-format/      node fields, body sections, handles, the rules, what follows from them
    apm-decompose/        what a good breakdown looks like: 3–7 children, no overlap,
                          explicit deps, testable acceptance, prefer sibling edges
    apm-deliver/          branch naming, trailer format, PR body, recording the delivery
  .mcp.json               { "mcpServers": { "apm": { "command": "apm", "args": ["mcp"] } } }
```

In your own terminal, `claude --plugin-dir "$(apm plugin-path)"` gives the session the same tools, skills and subagents, with `project` scope.

---

## 9. UI

The conversation is the main interface; the graph is what it is about. The [user guide](docs/user-guide.md) walks through all of it.

- **Top bar:** the project name; search (titles and bodies); counters that open lists: *Needs you* (an agent is waiting for your answer), *Ready*, *Proposals*, *Working*, *Problems*; and a light / dark / auto theme switch, remembered in the browser.
- **Contents** (left): the whole plan as a collapsible tree, with each node's state and progress.
- **Level view** (middle): the children of the current node, laid out left to right with elkjs (what a node needs is on its left) and drawn with React Flow. Double-click a node to go into it; the path above takes you back up. Click selects; shift-click selects several. Drag from one node to another to add a dependency.
  - **Node card:** state (including computed ready/blocked), handle, title, a child-progress bar, and markers for a running agent and a waiting proposal.
  - **Edges:** lifted edges show a count badge. Edges that leave the level show as dashed stubs naming the outside node. Entangled nodes are outlined under a banner.
  - **Proposals** show as dashed ghost nodes and edges on the level they would land in.
- **Right panel**, always there: an action bar on top, then two tabs, Chat and Details:
  - **Actions:** every operation as a button, for what is selected. One node: status, open, add child, add sibling, rename, switch kind, move, link, delete, Decompose, Implement. Several nodes: status, **merge**, delete. Nothing selected: add node, check plan, reconcile, commit hand edits, **export archive** (a download; including runs shows the secrets warning first).
  - **Chat:** the conversation with Claude, about the selected node or the whole plan, turn by turn. Past conversations are tabs. Every agent run appears here, so it is also where a decomposition or an implementation is followed. A question or permission prompt shows as a card with its options, or Allow and Deny (with a reason). The permission mode is picked under the message box.
  - **Details:** title (edited in place), state, description (markdown, with an editor), what it waits on, depends on (add and remove), is needed by and contains, delivery, and history. With nothing selected, a summary of the plan: progress, what is in progress, what is ready, problems.
- **Proposal review:** the rationale, the critic's review and the agent's warnings; you can edit any child, leave children out, and accept or reject.
- **Live:** every view refreshes when the plan changes, whoever changed it.
- **Look:** GitHub / VS Code. System font, neutral greys, one blue accent, 6px corners, hairline borders; colour only for state. Light, dark, or following the system.
- **Playback** (not built, M5): a timeline scrubber at the bottom. Scrubbing puts the view in read-only mode; delivery markers from the code repo sit along the timeline.

---

## 10. CLI (v1)

```
apm init [--name]                create .apm/, .apm-link, gitignore entries, first commit
apm serve [--port] [--no-open]   the web UI on this machine only; prints a tokenized URL
apm node add <title> [-p h] [--kind decision] [--dep h] [--intent "..."]
apm node ls [--root h] [--depth n]     outline view
apm node show <h> · set <h> [--title] [--kind] · status <h> <s> [--note]
apm node mv <h> <parent|--top> · merge <target> <others...> [--title] · rm <h> [-r] · edit <h>
apm link-dep <from> <to> · apm unlink-dep <from> <to>
apm ready [--root h] · apm search <query>
apm run <assistant|decomposer|implementer> [h] [-m "..."] [--mode m] [--no-critic]
apm proposal ls [--all] · show <id> · accept <id> [--only key] · reject <id>
apm reconcile                    mark nodes done whose PR or commits have merged
apm check                        validate invariants, report warnings (exit code 1 on errors)
apm commit                       validate and commit edits made by hand in .apm/
apm link --remote <url>          set the plan repo remote and record it in .apm-link
apm clone                        clone the plan repo named in .apm-link (for a fresh checkout)
apm push · apm pull              git push/pull for .apm
apm export <file> [--with-runs]  the whole project (plan + history, runs if asked) as one .apm.zip
apm import <file>                restore an .apm.zip into this folder, which must have no plan
apm mcp                          stdio MCP server (Claude Code starts it)
apm plugin-path                  where the Claude Code plugin is
```

`apm node ls`, `node show`, `ready` and `search` take `--json`. Every command accepts a handle: any unambiguous suffix of an id, at least 4 hex characters.

`apm run assistant` without a node is a chat about the whole plan. When a run needs an answer (a question, or permission for something), `apm run` asks in the terminal.

`apm node edit <h>` opens the body in `$EDITOR` and commits it as `edit-body` when you close the editor. If you edit files directly in `.apm/`, run `apm commit`: it validates the changes and commits the dirty files as `edit-body` (or `update` if `graph.ndjson` changed).

---

## 11. Build plan

| M | Deliverable | Exit criteria | State |
|---|---|---|---|
| **M1** | `core`: model, ndjson/md I/O, invariants, lifting, `mutate()` + lock, git commits with trailers | Property tests (Hypothesis) confirm I1–I4 hold across random op sequences; lifting and entanglement tests pass | **Built** |
| **M2** | `apm` CLI + `apm mcp` + plugin + skills | **Dogfood: plan APM itself** with the CLI and an interactive `claude` session | **Built**, except the SQLite index (§4). Dogfooding is yours to start |
| **M3** | `apm serve`: runner, decomposer + critic, proposals, SQLite runs; two-way runs with questions, permission prompts and modes; the chat | `apm run decomposer <h>` → proposal → `apm proposal accept` → one commit; the CLI checks in §7.1 are answered | **Built**; verified with real runs |
| **M4** | UI: level view, drill-down, actions, conversation, details, proposal review | Planning a feature entirely in the UI, start to finish | **Built**; verified in a browser with real runs |
| **M5** | Indexer frames + playback scrubber + delivery overlay | Replaying the APM plan from its first commit | Not built |
| **M6** | `apm-implementer` (worktrees, trailers, `gh` PR) | A ready leaf becomes a PR that links back to its node | **Built**; verified with a real run up to the commit. Opening a PR is untested (the test repo had no remote) |
| **M7** | Reconciling | A merged PR moves its node to `done` with no manual step | **Built** as `apm reconcile` (§7.5); the PR path is tested against a stand-in for `gh` |
| **M8** | Archives: `apm export` / `apm import`, Export archive… in the UI (§3.8) | A project, with its history and optionally its runs, round-trips through one `.apm.zip` | **Built**; round trip, refusals and the download are tested |

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
5. **Deleting a node with children.** Refuse it, or require `--recursive`? Proposed: refuse unless recursive; a recursive delete is one commit. The core implements the proposal for now.
6. **Reconciler split.** Decided: reconciling is plain code (`apm reconcile`, §7.5), with no agent. Still open: whether ambiguous cases (a PR closed without merging, a branch deleted) deserve an agent, or a flag for you to resolve.
7. **Cleaning up worktrees.** An implementer's worktree stays under `.claude/worktrees/` after its run. Should `apm reconcile` remove it once the node is done?
