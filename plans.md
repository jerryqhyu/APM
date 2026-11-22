# APM — Design Plan

**Status:** v10 · v1 is implemented except playback (M5) and the SQLite index; §11 has the state of each milestone · **Last updated:** 2026-11-22

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
| Reconciling | Plain code (`apm reconcile`), not an agent | "Did it reach the default branch?" has a definite answer: where the PR merged, or whether the commits (or the same changes) are there |
| Stacked work | A node may start on top of a dependency that is delivered but not merged (§2.3); `done` still means merged into the default branch | A chain of steps can be built and reviewed as stacked PRs without marking anything done early |
| Packaging | Agents + skills + MCP ship as one Claude Code plugin | The same plugin serves spawned runs and your own terminal sessions |
| Sharing a project | One `.apm.zip` archive (`apm export` / `apm import`, §3.8): the plan files plus a git bundle of the plan repo; runs only if asked | A whole project, history included, moves as one file with no remote to set up. Machine-specific state stays behind, and runs stay out by default because transcripts can hold secrets |
| Finishing a feature | Every node with children ends in a review step (kind `review`) that waits on its other children; tasks merge into a feature branch; the feature reviewer checks that branch against the feature's description before its PR goes on (§2.5, §7.6) | "All tasks merged" isn't "the feature works": a review against the spec catches the gaps between tasks. As a node in the graph, the review is ordered, visible and tracked like any work, and the feature branch keeps unreviewed work off `main` |
| Scope | Single user, single machine, one code repo (config already shaped for many repos) | Fastest route to a working slice |

---

## 2. Concepts

### 2.1 Nodes and levels

- Each node has **at most one parent** (`parent: null` = top level). Containment is a strict tree, so subtasks are never shared.
- A "level" is the set of children of one parent. You zoom into a node to see its children as their own DAG.
- There is no special root node. You can add a node at any level, at any time.
- Depth is computed, never stored.
- `kind`: `work` (default) | `decision` | `review`. A `review` node is a feature's **review step** (§2.5): APM adds and manages it, nobody else.

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

- **waiting on**: the dependencies that aren't `done`. Dependencies are inherited: a node also waits on whatever its ancestors wait on, so the children of a blocked parent are blocked too.
- **stacked on**: the part of *waiting on* that is *delivered*: an `in_progress` leaf whose delivery has a branch or a PR, so its work is pushed but not merged.
- **stack top**: the one of *stacked on* that new work builds on: the only one, or the one that depends (directly or not) on all the others, so its branch was built on theirs. None if they sit on separate branches. `apm.core.delivery.choose_base` uses the same rule (§7.4).
- **blocked**: `todo`, and waiting on something it can't stack on: a dependency that isn't delivered (including an `in_progress` parent, or a leaf with nothing pushed), or delivered dependencies on separate branches.
- **ready**: `todo`, not blocked, and not under a `dropped` ancestor. A ready node with a stack top is **ready, stacked**: shown as `todo·ready·stacked … · stacked on 7d2e0f11 (#20)` in text and "Ready · stacked on #20" in the UI. It can be implemented now; it becomes plainly ready once that work is `done`.
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
| I8 | A review step is a leaf inside a feature, and a feature has at most one | hard error |
| I9 | A node with children has a review step | warning (plans from before review steps; APM adds it the next time the feature's children change) |

`mutate()` runs I1–I4, I7 and I8 before every commit. The UI and `apm check` report I5, I6 and I9.

### 2.5 Features, review steps and feature branches

A node with children is a **feature**, and every feature ends in a **review step**: one more child, of kind `review`, titled "Feature review", that depends on all the feature's other children. It becomes ready only once they are all done; then the feature reviewer (§7.6) checks the **feature branch** against the feature's description. The review step is how a feature is finished: the feature can't reach 100% until it is done.

- **APM maintains it** (`apm.core.review_steps.maintain`, run by `mutate()` on every change before validation). A node that gets children, and isn't done or dropped, gets a review step. The step always depends on exactly its feature's other children that aren't dropped: tasks added later are added, removed or dropped ones removed. A step left alone (every task deleted) is removed, unless it has started. It is listed in the commit's `APM-Nodes` but isn't among the operation's `created` nodes.
- **Only APM manages it.** It can't be deleted (drop it to skip the review), moved, merged, linked or unlinked, or have its kind changed; no node can be made one, and nothing can go inside one. Renaming it and setting its status are fine. Merging two features keeps one step: the target's, else one of theirs (a started one first).
- **It comes last** among its siblings in every list and outline, whatever its id.
- **Feature branches.** A feature's tasks go into its branch, `feature/<handle>-<slug>`, which APM creates from the default branch (or, for a feature inside a feature, from the outer feature's branch) the first time one of its tasks is implemented, pushes to `origin`, and records as the review step's delivery: the step is a leaf, so it can carry one (I7). A task's pull request targets the feature branch, and the task is done once it merges there (§7.5). The step's own pull request is the feature branch going on to its **home**: the enclosing feature's branch, or the default branch. When that merges, the step is done, and so is the feature.

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
    .cache/                 apm.db, write.lock, server.json, task-review/<uuid>.json
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
automation:                  # what `apm serve` does on its own (§7.7); every switch defaults to on
  task_review: true
  reconcile: true
  reconcile_interval_s: 300
  feature_review: true
  accept_follow_ups: true
  implement_follow_ups: true
  feature_pr: true
  eager_redo: true           # rebuild the tasks after an eager task PR that changed (§7.9)
features:                    # how features are built and reported (§7.8)
  eager: false               # build a feature in one run, then split it into task PRs (§7.9)
  task_max_lines: 50         # the task size limit a task review applies, in real lines
  report: true               # write a feature report when its feature review passes
  report_detail: standard    # brief | standard | detailed
  overrides:                 # per feature, by node id (a handle typed by hand works too)
    0192f0c1-8a2b-7c3d-9e4f-1c0b9a44d2e7: { eager: true, report_detail: detailed }
```

`features.overrides` keys must have the form of an id or a handle; a handle of digits only goes in quotes, or YAML reads it as a number. An override that names no node, or a second one for the same node, is ignored, and `apm check` warns about it.

### 3.5 Commit format (plan repo)

Each semantic operation produces exactly one commit:

```
decompose 1c0b9a44: 4 children

APM-Op: decompose
APM-Nodes: <uuid>,<uuid>,<uuid>,<uuid>,<uuid>
APM-Actor: agent:apm-decomposer        # or user
APM-Run: <run-id>                      # optional
```

Ops: `create, update, move, delete, link, unlink, status, deliver, decompose, edit-body, merge, settings`. `settings` changes `apm.yaml`, not the graph (§7.8); a feature's override lists that feature in `APM-Nodes`, so it shows in its history. The plan repo's first commit is `init <name>`, with `APM-Op: init`.

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
task_reviews(node_id, fingerprint, verdict, data, run_id, created)
                                              -- one row per task review (§7.7)
eager_builds(feature_id PK, round, branch, base, base_sha, sha, state, tasks, run_id, error,
             warnings, pending, updated)      -- a feature's latest eager build (§7.9)
eager_slices(feature_id, node_id, position, branch, commit_sha, parent_sha, tree, patch_id,
             lines, landed)                   -- each task's part of it, in chain order
```

`task_reviews.fingerprint` is the SHA-256 of the task's title, kind and body (`content_fingerprint`), so a review applies to exactly one version of the content and a status change or delivery doesn't invalidate it. `verdict` is `pass`, `too_big`, `unclear` or `error`; `data` is what the reviewer returned with it. Task reviews are not archived: an imported project's tasks are simply reviewed again.

Eager builds aren't archived either: they describe branches in this machine's clone. Without them a split feature's tasks are still delivered and reconciled as usual; only redo detection is lost.

Deleting `.cache/` loses run logs, pending proposals, task reviews and eager builds, never anything about the project itself. They leave the machine only in an archive made with `--with-runs` (§3.8).

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
GET    /api/export?with_runs=               the project as an .apm.zip download (§3.8)
POST   /api/nodes · PATCH /api/nodes/:id · POST /api/nodes/:id/move · DELETE /api/nodes/:id
POST   /api/merge           { target, others[], title? }
POST   /api/edges · DELETE /api/edges
POST   /api/reconcile?retarget= · POST /api/commit    mark merged work done, report stacks · commit hand edits
GET    /api/settings · PATCH /api/settings  { features?, automation? }   apm.yaml's settings (§7.8)
PATCH  /api/nodes/:id/settings  { eager?, task_max_lines?, report?, report_detail? }   null = inherit
DELETE /api/nodes/:id/settings      a feature's own settings, cleared
POST   /api/nodes/:id/review-tasks · POST /api/nodes/:id/implement   { mode?, message? }
                            Review all tasks · Implement all: { started, waiting, skipped }
POST   /api/nodes/:id/eager/redo   { mode?, message? }   Redo on an eager feature (§7.9): the run
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
| `apm_ready(root?)` | Ready leaves: "what can be worked on now". Stacked ones are marked `·stacked` and name the branch to build on |
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

Limits: `max_concurrent` pipelines in total. One conversation takes one turn at a time; a node has one decomposer and one implementer at a time. A request beyond that is refused (HTTP 409), not queued; Review all tasks and Implement all queue theirs instead (§7.8). Stop = SIGTERM to the process group, then SIGKILL after 5s. A run still marked running whose host process has died is marked failed the next time a runner starts.

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
| `apm-implementer` | `node:<id>` | Read, Grep, Glob, Edit, Write, `Bash(git *)`, `Bash(gh pr *)`, its node's tools | `--worktree`; its first message names the base branch (§7.4) |
| `apm-eager-builder` | `read` | Read, Grep, Glob, Edit, Write, `Bash(git *)`, the read tools | `--worktree`; builds every task of an eager feature on its eager branch; only started by Implement or Redo (§7.9) |
| `apm-splitter` | `read` | Read, Grep, Glob, Edit, Write, `Bash(git *)`, the read tools | `--worktree`; carves an eager build into one commit per task and returns the chain (JSON schema); only started by the eager pipeline |

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

- **Only offered for a leaf that can start:** ready (every dependency done, or ready, stacked: §2.3), or already in progress.
- Spawned with `--worktree apm-<handle>-<slug>`, so it works in its own worktree under `.claude/worktrees/` and your checkout is never touched. `apm init` ignores that folder.
- Scope `node:<id>`.
- It works on branch `apm/<handle>-<slug>`. Commits carry `APM-Node:`. It opens the PR itself, with `gh pr create --base <base>`.
- It calls `apm_set_status(in_progress)` when it starts and `apm_attach_delivery` when the work is pushed, and ticks the acceptance criteria it verified in the node's body.
- **Never on a review step**: those run the feature reviewer (§7.6).
- **Not on a task of an eager feature**: Implement there starts the feature's eager build, or is refused once the feature is split (§7.9).
- **The base branch** is plain code (`apm.core.delivery.choose_base`), worked out when the run starts and written into its first message. A task inside a feature targets the **feature branch** (§2.5), created on demand; otherwise the default branch. Stacking then works within that target. The candidates are the delivery branches of what the node depends on (its own dependencies and its ancestors', and the leaves of a dependency that has children). `origin` is fetched first, and a candidate counts as *open* if its branch still exists and isn't merged (its PR isn't merged, and its tip isn't on the default branch).
  - One open candidate: the agent starts from it and the PR targets it, so stacked PRs each show only their own node's changes.
  - None: the default branch.
  - Several, one built on the others (`Graph.stack_top`, the rule §2.3 uses for "ready, stacked"): that one.
  - Several on separate branches: the default branch, and the first message names the unmerged ones so the PR can say what must merge first. A branch can only stack on one, and the node shows as blocked.
- A candidate's PR is read through the REST API (`read_pr`: `gh api repos/<o>/<r>/pulls/<n>`), so a squash-merged dependency counts as merged even where `gh pr` is refused.
- **The PR is the agent's job, with a fallback.** After a successful implementer turn, APM checks the node's delivery (`ensure_pull_request`):
  - A PR is recorded: nothing to do.
  - A branch but no PR: APM looks for an open PR from that branch and records it, or opens one through the GitHub REST API (`gh api repos/<owner>/<repo>/pulls`, which works where `gh pr`'s GraphQL calls are refused). The title is the node's title; the body is the node's body and its `APM-Node:` line; the base comes from `choose_base`; owner and repo come from the repo's `remote` in apm.yaml, else its `origin`.
  - If no PR can be opened, that is unfinished work, never something to mark done. The conversation gets a line from APM saying why, and, when a branch is waiting, the node's Notes say so too ("No pull request yet"). Details shows "Pull request: None yet". A turn that recorded no branch at all only gets the line in the conversation, since the agent may simply have stopped to ask something.

### 7.5 Reconciling

`apm reconcile` marks an in-progress node `done` when its work is where it belongs, its **home**: its feature's branch for a task inside a feature, the enclosing feature's branch or the default branch for a review step (§2.5), else the default branch. Below, "the default branch" means that home. When a review step becomes done, its feature does too, once all its other children are done or dropped. It also runs once when `apm serve` starts, and **Reconcile** in the UI runs it. It is plain code (`apm.core.reconcile`), and fetches `origin` first:

- **A node with a pull request** is done when the PR merged into the default branch, or into a branch whose merge commit has since reached the default branch. The PR is read through the REST API (`gh api repos/<o>/<r>/pulls/<n>`: `merged`, `base.ref`, `merge_commit_sha`, `state`), never `gh pr view`, whose GraphQL calls some environments refuse.
- **A PR merged into another branch** (a stack: #21 merged into #20's branch) leaves the node in progress. Reconcile reports "PR #21 merged into apm/… (#20's branch), not yet on main" and writes it under the node's Notes once.
- **Without a PR** (or when `gh` can't tell), the node is done when every delivery commit is on the default branch, or when the same changes are: each commit rebased (`git cherry`), or all of them squashed into one commit (the patch id of their combined diff matches one of the default branch's last 500 commits).
- **A PR closed without merging** is reported and left alone.
- **Retargeting.** This is also what moves an eager feature's chain of task PRs along as its tasks merge in order (§7.9). An open PR whose base is a branch that has landed (its tip is on the default branch, or the node that delivered it is done, or that node's PR merged into the default branch) is reported: "PR #21 still targets apm/… (#20's branch), which has landed: retarget it to main". `apm reconcile --retarget` (or `POST /api/reconcile?retarget=true`) points it at the default branch through `gh api -X PATCH`. Without the flag nothing on GitHub changes.
- If nothing can be determined (no `gh`, offline, a commit this clone doesn't have), the node is left alone.

### 7.6 Feature review

The feature reviewer (`apm-feature-reviewer`, label **Review feature**) checks a finished feature against its description. It runs on a feature's review step (started on the feature, it goes to the step), and only once every other child of the feature is done or dropped. It is read-only (`--disallowedTools Write,Edit,NotebookEdit`, read scope), may run `git log/diff/show/grep`, and returns JSON (`FEATURE_REVIEW_SCHEMA`):

```json
{"verdict": "complete|incomplete", "summary": "…",
 "criteria": [{"criterion": "…", "status": "met|partial|missing|unverified", "evidence": "file:line, test, command"}],
 "issues": [{"severity": "blocker|major|minor", "problem": "…", "where": "…", "suggestion": "…"}],
 "follow_ups": [ /* children, in the decomposer's shape */ ]}
```

The plain code around it (`apm.core.feature_review`):

- **What it reviews**: the feature branch recorded on the step, at its current tip on `origin`. A branch can be named instead (`apm run feature-reviewer <node> --branch`, or the run dialog). For a feature without a step (an older plan) APM works the branch out from the tasks' deliveries: the branch their PRs merged into, the top of their stack, or the default branch.
- **Where**: a detached checkout of that commit in `.claude/worktrees/apm-review-<handle>`, replaced by the next review of the feature, so the agent reads the code as built and your checkout is never touched. Its first message names the branch, the commit, and every task with whether its work is in that commit.
- **The verdict** is held to its own evidence: "complete" with a missing or partial criterion, or a blocker or major issue, is recorded as incomplete.
- **Recording**: the verdict, a table of the criteria and the issues go into the **review step's** description under `## Feature review` (the feature's description, the spec, is untouched). Follow-ups become a proposal on the **feature**, so accepting them adds tasks that the step then waits on. A later turn of the same conversation replaces its earlier pending proposal.
- **After**: incomplete sends the step back to `todo`. Complete opens the feature branch's PR into its home (title: the feature's; body: the feature's description and the review) and leaves the step `in_progress` until reconcile sees it merge; if the reviewed branch already is the home, the step and feature are done at once.

### 7.7 Task review and the automation loop

A task (a leaf that is not a review step) must fit in about five minutes of reading: at most the **task size limit** in real lines of source (`features.task_max_lines`, 50 by default, §7.8), not counting blank lines, comments, imports, tests, docs or agent/skill markdown. The reviewer's first message states the limit for its task; the decomposer's and the implementer's state it too. The task reviewer (`apm-task-reviewer`, with the `apm-task-sizing` skill, label **Review task**, `apm run task-reviewer <node>`) is read-only, reads the task and the code it will touch, and returns one verdict: `pass`; `too_big` with a split of at least two children, in the decomposer's shape; or `unclear` with a non-empty list of what is missing. Malformed output, a failed run or a run cut off by a restart is recorded as `error`.

- **Recording.** Each verdict is a `task_reviews` row (§4) keyed by the content fingerprint. `too_big` also becomes a proposal on the task; accepting it turns the task into a feature with those children and its review step.
- **The lock.** While a review runs, `.cache/task-review/<uuid>.json` holds the reviewer's pid, host and run id (`apm.core.review_lock`). Every plan operation that would edit, move, merge, delete, decompose or implement the task checks it and refuses with `TaskUnderReview` (HTTP 409); status changes and deliveries still go through. The marker is removed when the run ends however it ends; one whose pid is dead on this host doesn't count, so a crash never leaves a task locked.
- **The gate.** Implement (UI, API, CLI, MCP, automation) on a `todo` task requires its latest review to be `pass` for its current fingerprint. `in_progress` and `done` tasks are not gated.
- **Retries.** After an `error`, APM retries once per fingerprint (`Store.count_errors`), then waits for an edit or a review started by hand.

**The automation loop** runs inside `apm serve`, each step behind its own `automation` switch (§3.4):

1. `task_review`: creating a task or changing its title, kind or description, through any entry point, queues a review. A queue debounces bursts into one review of the final content. At startup, every `todo` task without a review for its current fingerprint is queued.
2. `reconcile`: reconcile with retarget (§7.5) every `reconcile_interval_s` seconds.
3. `feature_review`: when a feature's review step becomes ready, its feature review (§7.6) starts.
4. `accept_follow_ups`: the follow-ups an incomplete review proposes are accepted, and so task-reviewed.
5. `implement_follow_ups`: those follow-ups (and only tasks created that way) are implemented once they pass, one PR each into the feature branch; once they merge, step 3 reviews the feature again.
6. `feature_pr`: a complete review opens the feature's PR into its home. Off, the reviewed commit is recorded and no PR is opened.

Automated runs share `runner.max_concurrent` with everything else, waiting in the queue rather than failing for lack of a slot, and each gets its agent's `budget_usd`.

### 7.8 Feature settings and feature-level actions

**Settings.** `features:` in `apm.yaml` (§3.4) holds the project's defaults for every feature and per-feature overrides. `apm.core.settings.feature_settings(config, graph, node)` is the one place that works out what applies to a node: the defaults, then each enclosing feature's override from the outside in, then the node's own. A task gets its features' settings. The task review, the decomposer and the implementer read `task_max_lines` through it.

- `eager`: build a feature in one run on one branch, then split it into one pull request per task (§7.9). `apm.runner.feature_runs.implement_feature` is where it plugs in.
- `report`, `report_detail`: write a feature report once the feature review passes. Not built yet.

**Changing them.** `set_defaults`, `set_override` and `clear_override` edit `apm.yaml` in place with ruamel.yaml's round trip (`apm.core.config_edit`): comments, indentation and the spelling of untouched values stay. The result must differ from the original by exactly the change asked for, read the way `parse_config` reads it, and must pass `parse_config`; otherwise nothing is written. Each change is one `settings` commit under the write lock (§3.5). Hand edits to `apm.yaml`, which `apm commit` doesn't take, are committed with it, and the commit says so. A change of the limit doesn't undo passed task reviews: Review all tasks reviews a feature's tasks again.

**Review all tasks** (`apm review-tasks <feature>`) starts a task review on every To do task in the feature, nested features included, but not review steps; tasks under review are skipped. **Implement all** (`apm implement <feature>`) starts the implementer on every task that is ready now, stacked ones included, and passes the Implement gate; blocked tasks, and tasks that fail the gate or are under review, are skipped with the reason. On a task, either covers that task.

Both go through a `RunQueue` (`apm.runner.queue`): what fits under `max_concurrent` starts at once, the rest waits, oldest first. In `apm serve` the automation loop pumps it ahead of its own queue; it is separate from that queue, so a run asked for by hand never counts as the automatic run of a version. A queued run that can no longer start when its turn comes (the node is gone or changed, or it is already running) is dropped. In the terminal, the command pumps the queue itself and follows every run, each line tagged with its node's handle.

### 7.9 Eager mode: one build, one pull request per task

With `features.eager` on for a feature (§7.8), Implement doesn't start one implementer per task. One agent builds every To do task of the feature in a single run, on one branch, and APM splits that branch into one branch and one pull request per task. The plain code is `apm.core.eager` (scope, gate, recording, redo detection) and `apm.core.split` (the git side); the runs are `apm.runner.eager`, one pipeline in the runner's slot for `apm-eager-builder` on the feature, so it counts once under `max_concurrent`.

```
gate ─▶ eager branch ─▶ apm-eager-builder ─▶ split ─▶ verify ─▶ push task branches ─▶ record ─▶ PRs
                        (one run, worktree)   │         │        (atomic, never forced)  (APM)    (chained)
                                              │         └─ fails: chat + feature Notes, nothing delivered
                                              └─ APM itself when the commits are task by task, else apm-splitter
```

- **Scope.** A build covers the feature's own To do tasks (not a nested feature's, which builds into its own branch), in dependency order (`dependency_order`: each after everything it depends on inside the build, otherwise outline order).
- **The gate** (`build_blockers`): every task passes its task review for its current content (`implement_blocker`), isn't under review or being implemented, and everything it needs from outside the build is done, since the build starts from the feature branch. Otherwise nothing starts, and each task says why (Implement all lists them as skipped; Implement on one task is refused with the list).
- **Where it plugs in.** Implement all on an eager feature (`implement_feature`) queues the build instead of implementers, for it and for any eager feature inside it. Implement on one To do task of an eager feature with no split yet starts that feature's build, the way Review feature on a feature runs on its review step (`Runner.start` → `redirect`); once the feature is split, it is refused ("its tasks come from the split; use Redo"), except for a task that isn't part of the split (added since, e.g. a review follow-up), which is implemented as usual.
- **The build.** APM makes the eager branch, `eager/<handle>-<slug>` (`-2`, `-3`, … for later rounds, so no build reuses a branch), from the feature branch's tip (`ensure_feature_branch`), and pushes it. `apm-eager-builder` (worktree `apm-eager-<handle>-<round>`, may edit, `Bash(git *)`, read-only plan) gets every task in order: title, handle, full id, description with its acceptance criteria, what it depends on, its size limit, and the branch it will get. It is told to commit task by task, each commit ending in `APM-Node: <id>`, to run the repository's tests and linters before handing over (as the implementer is), and to push the eager branch and nothing else.
- **The split.** If every commit from the base to the eager branch's tip names exactly one task, each task's commits come one after another, every task has some, and the order respects the dependencies, the split is deterministic (`mechanical_split`): those commits are the chain, a task's several commits squashed into one with `git commit-tree`, no agent involved. This is the case the builder is told to produce, so it is the usual one. Otherwise (one big commit, mixed commits) `apm-splitter` (worktree `apm-split-<handle>-<round>`, `Bash(git *)`, may edit to carve a file between tasks) makes the chain locally, without pushing, and returns it (`SPLIT_SCHEMA`: per task, in order, its `node_id`, `branch`, `commit`, `within_limit`, `note`). Worktrees share the object store, so APM reads its commits directly.
- **Verification** (`check_chain`), however the chain was made: one commit per task and each task once, in an order the dependencies allow, each commit's only parent the one before (the first's the base), each with its task's `APM-Node` trailer, none empty, and the last one's tree exactly the eager tip's. A chain that fails is reported in the chat and under the feature's Notes, nothing is pushed or recorded as delivered, and the build is kept: Redo splits it again.
- **Recording** (`record_split`, by APM, not the agent): the task branches, `apm/<handle>-<slug>` like the implementer's, are pushed in one atomic, plain push (`publish`: a branch that already exists with other content is never overwritten). Each task gets its delivery (branch, commit) and goes In progress; `eager_slices` records each task's branch, commit, parent, tree, the patch id of its own change and a rough line count. Parts that look larger than their task's `task_max_lines` (APM's count of non-blank added source lines), and parts the splitter says it couldn't keep within it, are shown as warnings.
- **The PR chain.** APM opens one pull request per task through `ensure_pull_request` with an explicit base: the first targets the feature branch, each later one the task before it, so each shows only its own part. Its description says which part it is and to merge in order. `apm reconcile --retarget` (run by `apm serve`, §7.7) already does the rest: when task k merges into the feature branch, it is done (its home is the feature branch, §7.5) and task k+1's PR, whose base has landed, is pointed at the feature branch. A PR merged into the task before it is "merged elsewhere" and becomes done once that one lands.

**Redo.** A task's pull request *changes* when its branch gets new content after the split (review fixes), or when it merges into the feature branch with content other than its slice (edited during review, or squashed with changes). `find_changes` is the one place that decides, comparing what the split recorded with the branch tips (as fetched) and the PRs' merges:

- merged into the feature branch: as split if the merge commit's tree is the slice's tree, or the change the merge brings (first parent to merge) has the slice's patch id (a squash, a rebase, a merge on top of other work); such a task is marked *landed*. Otherwise it changed;
- merged into the task before it: left to that branch's check;
- open: changed if the branch's tip moved to a different tree, unless the new tip merely merged a later task into it (it holds that task's commit, and has its tree).

After every periodic reconcile in `apm serve` (`AutoQueue.reconcile`), `detect_redos` records on each split build what changed (the UI shows the tasks after it as stale), and with `automation.eager_redo` queues a redo, once per change (its signature is task, kind and tip). With the switch off nothing starts on its own; Redo (UI, `POST /api/nodes/:id/eager/redo`, `apm eager redo`) works either way.

A redo (`plan_redo`) starts from the earliest change: from the changed task's new tip, or from the feature branch's tip if it merged. It refuses, and says why on the feature, when it would have to touch what it must not: a task after the change that has merged or is done, a second changed task after it (commits APM didn't make), or a deleted branch. Otherwise a new round's eager branch starts at that base, the builder is told what changed (the diff between the split slice and what is there now) and rebuilds only the tasks after it, and the split runs as above on that base. Then each rebuilt task's branch **moves forward**, never rewritten (`rebuilt_commits`): APM adds a merge commit whose parents are the branch's old tip and the rebuilt task before it (the base for the first), and whose tree is the new part. The push is a plain fast-forward from where APM left the branch, so a commit someone else pushed meanwhile makes git refuse it, and nothing is forced anywhere. Each pull request's diff against the one before is again exactly its new part; the first rebuilt one is retargeted to the feature branch if the change merged. The change is then accepted (a pushed tip becomes the task's recorded commit; a merge marks it landed). Done and merged tasks are never touched. A redo that finds nothing after the change to rebuild just accepts it.

**What the UI shows.** A feature's details carry `eager`: the build (round, branch, base and its commit, state, error, warnings), each task's part in chain order with its pull request and state (open, merged, or stale: will be rebuilt), the pending change or why it can't be redone, and what Redo would do. A task's details say whether Implement builds its whole feature or why it can't start on its own.

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
    apm-feature-reviewer.md  review step → checks the feature branch against the feature's
                          description, criterion by criterion (read-only, json-schema)
    apm-task-reviewer.md  task → pass, too_big (with a split) or unclear (read-only, json-schema)
    apm-eager-builder.md  eager feature → every task on one branch, one commit per task (§7.9)
    apm-splitter.md       eager build → a chain of one commit per task (json-schema)
  skills/
    apm-node-format/      node fields, body sections, handles, the rules, what follows from them
    apm-decompose/        what a good breakdown looks like: 3–7 children, no overlap,
                          explicit deps, testable acceptance, prefer sibling edges
    apm-deliver/          branch naming, trailer format, PR body, recording the delivery
    apm-task-sizing/      what counts as a real line, the task size limit, how to split a task
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
  - **Actions:** every operation as a button, for what is selected. One node: status, open, add child, add sibling, rename, switch kind, move, link, delete, Decompose, Implement. Several nodes: status, **merge**, delete. A feature: also **Review all tasks** and **Implement all**, with what started, what waits for room and what was skipped. Nothing selected: add node, check plan, reconcile, commit hand edits, **export archive** (a download; including runs shows the secrets warning first), and **Settings…**: the `features:` defaults and the `automation:` switches, saved to `apm.yaml`.
  - **Chat:** the conversation with Claude, about the selected node or the whole plan, turn by turn. Past conversations are tabs. Every agent run appears here, so it is also where a decomposition or an implementation is followed. A question or permission prompt shows as a card with its options, or Allow and Deny (with a reason). The permission mode is picked under the message box.
  - **Details:** title (edited in place), state, description (markdown, with an editor), for a feature its settings (each with where it comes from: the project, an enclosing feature, or set here; Inherit, or a value of its own) and its eager build (the branch, each task's part and pull request in merge order, what is stale, Redo; §7.9), what it waits on, depends on (add and remove), is needed by and contains, delivery, and history. With nothing selected, a summary of the plan: progress, what is in progress, what is ready, problems.
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
apm run <assistant|decomposer|implementer|feature-reviewer> [h] [-m "..."] [--mode m] [--no-critic] [--branch b]
apm review-tasks <h> [--mode m]  a task review on every To do task in a feature
apm implement <h> [-m "..."] [--mode m]   the implementer on every ready, reviewed task in a feature;
                                 on an eager feature, its eager build (§7.9)
apm eager show <h> · redo <h> [-m "..."] [--mode m]   an eager feature's build and PR chain · Redo
apm settings show [h] · set NAME=VALUE... [--node h] · clear <h>   apm.yaml's settings (§7.8)
apm proposal ls [--all] · show <id> · accept <id> [--only key] · reject <id>
apm reconcile [--retarget]       mark nodes done whose work is on the default branch; report
                                 (or retarget) stacked PRs whose base has landed
apm check                        validate invariants and apm.yaml, report warnings (exit 1 on errors)
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
| **M6** | `apm-implementer` (worktrees, trailers, PR against the right base, APM's fallback) | A ready leaf becomes a PR that links back to its node, stacked on unmerged work it builds on | **Built**; verified with real runs up to the push (the PRs of the archive stack, #20–#24, were opened by hand from a container without `gh pr`, which is what the fallback now covers). Base selection and the fallback are tested against a local `origin` and a stand-in for `gh` |
| **M7** | Reconciling, stack-aware | Work reaching the default branch moves its node to `done`, with no manual step and no early `done` for stacked PRs | **Built** as `apm reconcile` (§7.5), with "ready, stacked" (§2.3). Tested against local repos and a stand-in for `gh` |
| **M8** | Archives: `apm export` / `apm import`, Export archive… in the UI (§3.8) | A project, with its history and optionally its runs, round-trips through one `.apm.zip` | **Built**; round trip, refusals and the download are tested |
| **M9** | Review steps, feature branches and the feature reviewer (§2.5, §7.6) | A feature's tasks merge into its branch; its review step checks the branch against the description and opens the PR on | **Built**; tested with stand-ins for `claude` and `gh` |
| **M10** | Feature settings and feature-level actions (§7.8) | Settings changed from the UI or the CLI land in `apm.yaml` with its comments kept; the task limit reaches the agents; Review all tasks and Implement all never fail on `max_concurrent` | **Built**, except the feature report, whose settings are in place; tested with a stand-in for `claude` |
| **M11** | Eager mode (§7.9) | A feature is built in one run, split into a verified chain of one pull request per task, and its later tasks are rebuilt when a task's pull request changes, without rewriting a branch | **Built**; the build, the split (deterministic and by agent), verification, the PR chain, reconcile along it and redo are tested with stand-ins for `claude` and `gh` against real git repos |

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
6. **Reconciler split.** Decided: reconciling is plain code (`apm reconcile`, §7.5), with no agent. A PR closed without merging is now reported; it stays open whether that, or a deleted branch, deserves more (an agent, or a flag on the node).
7. **Cleaning up worktrees.** An implementer's worktree stays under `.claude/worktrees/` after its run. Should `apm reconcile` remove it once the node is done?
