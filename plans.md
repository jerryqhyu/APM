# APM — Design Plan

**Status:** v4 draft, scoped for v1 · phased implementation plan in §12 · **Last updated:** 2026-09-22

APM is a local-first project manager built for working with AI agents. A project is a **nested graph of nodes**: you start with a big idea, then break it down with Claude agents, level by level, until each piece is small enough to ship as a commit or PR. The plan lives in its own git repo, so every change to it can be traced and **played back** from the project's first node onward.

v1 = TypeScript, one user on one machine, one code repo, Claude CLI integration, a local web UI.

---

## 1. Decisions

| Topic | Decision | Why |
|---|---|---|
| Language | TypeScript end-to-end, Node 24 LTS | One toolchain for core, server, MCP, UI; best Claude/MCP SDK support; `npx` install |
| Graph model | Compound graph: containment **tree** + dependency **DAG**; cross-level edges stored once and *lifted* for display | Unlimited depth, zoom into any node, no hand-copying edges between levels |
| Plan storage | Separate git repo at `.apm/`, ignored by the code repo and linked to it by a committed `.apm-link` | No CI runs on plan commits; works with a protected `main`; history holds only plan changes |
| Source of truth | `graph.ndjson` (structure + metadata) + `nodes/<id>.md` (prose only) | Each field lives in exactly one place, so the two can't drift |
| IDs | UUIDv7; the short handle is the **last** 8 hex chars | Sorts by creation time; new nodes append at the end of the file, so diffs stay small |
| Local DB | SQLite (`node:sqlite`) in `.apm/.cache/`, not committed | Queries, replay frames, run logs. Can be deleted at any time |
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
- Hand-written lines may be in any order, use `null` for absent fields, and leave out `kind`/`status` (defaults: `work`/`todo`). The next write puts them in canonical form.
- Unknown keys are errors, so a typo can't be silently dropped. IDs must be lowercase UUIDv7s; titles must be one non-blank line.

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

Commits are made through the `git` CLI (`execFile`), so your identity, signing, and hooks apply.

### 3.6 Code-repo trailer

Every delivery commit in the code repo carries `APM-Node: <full-uuid>`. This is how code links back to the plan, and it's what the reconciler reads.

### 3.7 IDs and handles

- Stored IDs are full UUIDv7 values.
- The **handle** is the last 8 hex chars (`1c0b9a44`). All CLI and MCP inputs accept an unambiguous handle suffix of at least 4 hex chars, or a full ID (with or without hyphens, any case).
- Those 8 chars are 32 random bits, so two nodes can share them (about a 1% chance somewhere in a 10,000-node project). Where that happens, both are displayed with as many extra chars as it takes to tell them apart.
- ⚠ Do **not** use the leading characters. In UUIDv7 those are the timestamp, so nodes created within about a minute of each other share their first 8 characters.

---

## 4. Local index (SQLite)

`.apm/.cache/apm.db`, using `node:sqlite` (fallback: `better-sqlite3`). WAL mode.

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
                ┌──────────────────── @apm/core ─────────────────────┐
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

### 6.1 Packages (pnpm workspace, one published binary)

```
packages/core   graph, invariants, lifting, git, index, mutate
packages/app    the `apm` binary: CLI commands + `serve` + `mcp`
packages/ui     vite + react + @xyflow/react + elkjs
plugin/         Claude Code plugin (agents, skills, .mcp.json)
```

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
| **M1** | `core`: model, ndjson/md I/O, invariants, lifting, `mutate()` + lock, git commits with trailers | Property tests (fast-check) confirm I1–I4 hold across random op sequences; lifting and entanglement tests pass |
| **M2** | `apm` CLI + `apm mcp` + plugin skeleton + skills | **Dogfood: plan APM itself** with the CLI and an interactive `claude` session |
| **M3** | `apm serve`: runner, decomposer + critic, proposals, SQLite runs | `apm run apm-decomposer <h>` → proposal → `apm proposal accept` → one commit; the three M3 CLI checks in §7.1 are answered |
| **M4** | UI: level view, drill-down, node panel, live run stream, proposal review | Planning a feature entirely in the UI, start to finish |
| **M5** | Indexer frames + playback scrubber + delivery overlay | Replaying the APM plan from its first commit |
| **M6** | `apm-implementer` (worktrees, trailers, `gh` PR) | A ready leaf becomes a PR that links back to its node |
| **M7** | `apm-reconciler` | A merged PR moves its node to `done` with no manual step |

Each milestone is split into PR-sized phases in §12.

---

## 12. Implementation phases

Each phase is **one branch → one PR into `main`**. Phases are small enough to review in one sitting, and each leaves `main` green and usable. A milestone is complete when its last phase merges and the milestone's exit criteria (§11) are met.

### 12.1 Conventions (every phase)

- **Branch:** `phase/NN-<slug>` (e.g. `phase/02-model-io`). Squash-merge into `main`.
- **PR title:** `P<NN>: <phase title>`. **PR body:** goal, scope delivered, the phase's exit criteria as a ticked checklist, anything deferred, and follow-ups.
- **CI must pass:** typecheck, lint, and tests (from P01 onward).
- **Keep the plan true:** each PR updates its row in the tracker (§12.2), and edits the design sections if the implementation diverged from them. The plan doc is never allowed to drift from the code.
- **No dead surface:** a command, endpoint or tool is registered only once it works. Nothing ships as a "not implemented" stub.
- **Tests live with the code:** unit and property tests in the package; end-to-end tests run the built `apm` binary against throwaway git repos in a temp dir.
- From **P09** onward, phase status is also tracked in APM itself (dogfooding). This file stays the design reference.

### 12.2 Tracker

| Phase | Title | M | Depends on | Status | PR |
|---|---|---|---|---|---|
| P00 | Phased implementation plan (this doc) | — | — | done | [#1](https://github.com/jerryqhyu/APM/pull/1) |
| P01 | Workspace scaffold + CI | M1 | P00 | done | [#2](https://github.com/jerryqhyu/APM/pull/2) |
| P02 | Node model, IDs and file I/O | M1 | P01 | in review | [#3](https://github.com/jerryqhyu/APM/pull/3) |
| P03 | Invariants and computed status | M1 | P02 | todo | |
| P04 | Lifting and level views | M1 | P03 | todo | |
| P05 | `mutate()`, write lock, git commits, `init` | M1 | P03 | todo | |
| P06 | SQLite index + FTS | M2 | P05 | todo | |
| P07 | `apm` CLI | M2 | P04, P06 | todo | |
| P08 | `apm mcp` + plugin skeleton + skills | M2 | P07 | todo | |
| P09 | Dogfood: plan APM in APM | M2 | P08 | todo | |
| P10 | `apm serve`: HTTP/WS core and security | M3 | P07 | todo | |
| P11 | Runner: spawning `claude`, run logs, cancel | M3 | P10 | todo | |
| P12 | Decomposer, critic and proposals | M3 | P11 | todo | |
| P13 | UI shell + level view | M4 | P10 | todo | |
| P14 | UI node panel + editing | M4 | P13 | todo | |
| P15 | UI runs + proposal review | M4 | P12, P14 | todo | |
| P16 | Frames index + history API | M5 | P10 | todo | |
| P17 | Playback scrubber + delivery overlay | M5 | P15, P16 | todo | |
| P18 | `apm-implementer` | M6 | P15 | todo | |
| P19 | `apm-reconciler` | M7 | P18 | todo | |
| P20 | v1 release: package name, docs, publish | — | P19 | todo | |

**Parallel tracks:** after P03, P04 and P05 can proceed in parallel. After P10, three tracks can run side by side: the runner (P11 → P12), the UI (P13 → P14) and history (P16). They join again at P15 and P17.

### 12.3 Phases

#### M1: core

**P00 — Phased implementation plan**
- Commit this document to the repo and add the phase breakdown.
- *Exit:* merged; the tracker exists.

**P01 — Workspace scaffold + CI**
- pnpm 12 workspace with `packages/core` and `packages/app`. `plugin/` is added in P08 and `packages/ui` in P13, so that neither lands as an empty placeholder. Node 24 is pinned via `engines` and `.nvmrc`, and the workspace uses the `packageManager` field.
- TypeScript 7 in strict mode, ESM only. `tsc -b` with project references builds `core` and `app` to `dist/` for publishing; a root `tsconfig.json` typechecks sources, tests and configs in one `noEmit` pass.
- **Run from source, no build step in development.** Node 24 strips types natively, so the code keeps to erasable TypeScript (`erasableSyntaxOnly`) and imports relative files with `.ts` extensions, which `tsc` rewrites to `.js` on build. Workspace packages export `./src/index.ts`; `publishConfig.exports` points at `dist/` for publishing. Node, Vitest and `tsc` all resolve `@apm/core` to its source.
- Tooling (confirmed): **Vitest** + **fast-check** for tests (colocated `*.test.ts`), **Biome** for lint and format.
- `apm` binary entry in `packages/app` that implements only `--version` and `--help`.
- GitHub Actions workflow on PRs and `main` to run install (frozen lockfile), lint, typecheck, build, test, and a smoke test of the built binary. It runs on `ubuntu-latest` and `macos-latest`, because the file locking and git behaviour tested later differ between them.
- *Exit:* a fresh clone runs `pnpm i && pnpm build && pnpm test` green; CI is green on the PR; `pnpm apm --version` prints the version.

**P02 — Node model, IDs and file I/O** (§3.2–3.4, §3.7). *Decided:* the format rules now in §3.2, handle input and collisions in §3.7, and `delivery.pr` as a URL.
- Types: `Node`, `Kind`, `Status`, `Delivery`, plus the reserved `external` and `variant_of` fields (parsed and preserved, otherwise unused).
- UUIDv7 generation, `handle()` (the last 8 hex chars) and `resolveHandle(suffix)`, which errors on an ambiguous or unknown suffix.
- `graph.ndjson` serializer: sorts by `id`, writes keys in a fixed order, and omits null and empty values. The parser reports errors with line numbers.
- `nodes/<id>.md` read/write, and the body template for new nodes (Intent / Acceptance criteria / Notes).
- `apm.yaml` schema (zod + `yaml`) with defaults.
- *Exit:* property test shows `parse(serialize(g)) ≡ g`, and that serialization is byte-stable (serializing twice gives identical bytes); golden-file tests pass; handle-ambiguity tests pass.

**P03 — Invariants and computed status** (§2.3, §2.4)
- An in-memory `Graph` with a children index, ancestor walk, and computed depth.
- `validate(graph)` returns structured `errors` (I1–I4, I7) and `warnings` (I6), each with a code and the node IDs involved.
- Computed state: `blocked`, `ready`, `progress` (over leaves that aren't dropped), and the dropped-dependency flag.
- *Exit:* one or more unit tests per invariant; a fast-check generator of valid graphs; derived-status tests, including a dependency on a `dropped` node.
- *Decides:* open question 2 (parent status). The v1 default stays: parent status is stored, and I6 raises a warning.

**P04 — Lifting and level views** (§2.2, I5)
- `levelView(graph, root | null)` returns the children of `root`, the lifted edges with their counts and underlying edges, and boundary ports (incoming and outgoing, each naming the outside node).
- Entanglement (I5): cycle detection on the lifted edges of each level, reporting the nodes in the cycle. `checkAll()` runs it at every level for `apm check`.
- *Exit:* fixture tests cover deep cross-level edges, merged duplicates and ports; a property test confirms every underlying edge is accounted for **exactly once** at each level (drawn, internal or port).

**P05 — `mutate()`, write lock, git commits, `init`** (§3.1, §3.5, §6.2)
- Git wrapper over `execFile` providing `add`, `commit` (with trailers), `rev-parse`, `show`, `log` and `is-ancestor`.
- `.cache/write.lock`: created with `O_EXCL` and holding the PID, hostname and a timestamp. It is stale if the PID is dead. Acquisition retries with a timeout.
- `mutate(op, fn)` follows the 5 steps in §6.2 and emits `graph.changed` in-process. Ops: `create, update, move, delete, link, unlink, status, deliver, decompose, edit-body`.
- `initProject()` creates `.apm/` (`git init -b main`), `apm.yaml`, an empty `graph.ndjson`, and `.apm/.gitignore`; writes `.apm-link`; appends `.apm/` to the code repo's `.gitignore`; and makes the first commit.
- Project discovery: `APM_PROJECT`, otherwise walk up from the cwd to find `.apm-link`.
- *Decides:* whether deleting a node with children is refused or requires `--recursive` (proposed: refuse unless recursive; a recursive delete is one commit).
- *Exit (= M1):*
  - A property test over random op sequences shows I1–I4 hold after every committed op, and that a rejected op leaves HEAD and the working tree untouched.
  - A multi-process test with N processes calling `mutate()` concurrently produces N commits and loses no updates.
  - A stale lock is recovered.
  - Trailers round-trip through `git log`.

#### M2: CLI, MCP, dogfood

**P06 — SQLite index + FTS** (§4)
- A `node:sqlite` wrapper behind a small interface, so `better-sqlite3` can be swapped in. WAL mode; migrations keyed by `schema_version`.
- Tables `nodes`, `edges`, `closure`, `nodes_fts` and `meta` only. `frames` lands in P16; `runs`, `run_events` and `proposals` land in P11–P12.
- `ensureFresh()` compares HEAD with `last_indexed_sha` and rebuilds on a history rewrite. `mutate()` updates the index after each commit. `search(query)`. `reindex()`.
- *Exit:* deleting `.cache/` and then reading rebuilds an identical index; a history rewrite is detected; FTS covers titles and bodies.

**P07 — `apm` CLI** (§10)
- Every §10 command except `serve`, `mcp`, `run` and `proposal`, which arrive with their subsystems. Includes `apm commit` for hand edits (described in §10's prose but missing from its command list).
- A shared outline formatter (`1c0b9a44 [work/todo·ready] Title → 7d2e0f11`), reused by the MCP in P08.
- `node edit` via `$EDITOR`. `link --remote`, `clone`, `push` and `pull` wrap git.
- Non-zero exit codes on errors, and `--json` output on read commands for scripting.
- *Exit:* end-to-end tests drive the built binary through init → add → nest → link → status → check → commit → reindex in a temp repo.

**P08 — `apm mcp` + plugin skeleton + skills** (§6.4, §8)
- A stdio server on `@modelcontextprotocol/sdk` that exposes every §6.4 tool, with zod input schemas and outline-style output.
- `APM_RUN_SCOPE` enforcement for `read`, `node:<id>` and `project`.
- `plugin/`: `.claude-plugin/plugin.json`, `.mcp.json`, and the `apm-node-format` and `apm-decompose` skills. The agent definitions land with their phases (P12, P18, P19).
- *Exit:* an in-process MCP client calls every tool; every scope has tests for both allowed and refused calls; `claude --plugin-dir ./plugin` lists the APM tools and skills.

**P09 — Dogfood: plan APM in APM**
- Run `apm init` in this repo. The plan repo gets its own private remote (to be created by you). This PR commits only `.apm-link` and the `.gitignore` entry to the code repo.
- Enter the remaining phases (P10+) as nodes through an interactive `claude` session with the plugin, with milestones as the top-level nodes and phases as their children.
- Fix the problems found while dogfooding, and list them in the PR.
- *Exit (= M2):* the APM plan is maintained in APM; `apm ready` shows the next phase.

#### M3: server, runner, proposals

**P10 — `apm serve`: HTTP/WS core and security** (§6.3)
- HTTP server + `ws` (proposed: Hono on `@hono/node-server`), bound to `127.0.0.1`. A random token is written to `.cache/server.json`, and every request must carry it: as a header, or as a query parameter for the WS upgrade. `Origin` is checked on every request.
- Node and edge endpoints: `GET /api/graph?root=` (the `at=` parameter is added in P16), `GET/POST/PATCH/DELETE /api/nodes…`, `POST /api/nodes/:id/move`, `POST/DELETE /api/edges`.
- WS `graph.changed`, which also fires for commits made by *other* processes (the CLI or MCP) by watching `.apm` refs, with a polling fallback.
- `apm serve [--port]` prints the tokenized URL.
- *Exit:* the security tests reject a missing or wrong token, a foreign `Origin` and a non-loopback bind; a CLI mutation shows up as a WS event.

**P11 — Runner: spawning `claude`, run logs, cancel** (§7.1, §7.3)
- `runs` and `run_events` tables. A per-run MCP config file injects `APM_PROJECT` and `APM_RUN_SCOPE`. The spawn flags are exactly those in §7.1, with the model and budget taken from `apm.yaml`.
- stream-json line parser → `run_events` → WS `run.event` / `run.finished`.
- Limits: `max_concurrent` runs in total and one active run per node (v1 rejects extra runs with 409 rather than queueing them). Cancel sends SIGTERM, then SIGKILL after 5s.
- `POST/DELETE /api/runs`, resume via `--resume <session>`, and `apm run <agent> <h>`, which calls the server and tails the stream in the terminal.
- *Exit:* tests against a **fake `claude` script** that emits canned stream-json cover success, failure, cancel and the limits. A real run answers the three "to check at M3" questions in §7.1, and the answers are written back into §7.1.

**P12 — Decomposer, critic and proposals** (§7.2)
- `plugin/agents/apm-decomposer.md` and `apm-critic.md`, the decomposition JSON schema, and the critic's output schema.
- Pipeline: decomposer → critic → at most one revision → a `proposals` row → WS `proposal.created`.
- Accept (with optional edits): assign UUIDv7s, resolve keys, enforce additive-only, write bodies from `intent` and `acceptance`, then make one `decompose` commit with `APM-Actor` and `APM-Run` trailers. Reject.
- `POST /api/proposals/:id/accept|reject` and `apm proposal ls|show|accept|reject`.
- *Decides:* open question 1 (the policy for near-duplicates flagged by the critic).
- *Exit (= M3):* fixture tests cover bad keys, cycles introduced by a proposal (rejected) and additive-only violations. A real run of `apm run apm-decomposer <h>` → `apm proposal accept` produces exactly one commit.

#### M4: UI

**P13 — UI shell + level view** (§9)
- `packages/ui` (Vite + React + `@xyflow/react` + elkjs). `apm serve` serves the built UI; in development, Vite proxies requests to the server. The token is read from the URL and kept in `sessionStorage`.
- Level view: elkjs layered layout; node cards (handle, title, stored and computed status, progress bar, has-children marker); lifted-edge count badges; boundary ports; entanglement banner; double-click to drill down; breadcrumb; live refresh over WS.
- The view is read-only in this phase.
- *Exit:* component tests for cards, badges and ports; a Playwright smoke test in CI loads a fixture project and drills down two levels.

**P14 — UI node panel + editing**
- Node panel: markdown body editor, dependencies (add and remove), delivery display, history (the node's `git log`), and set status. Actions: add child, delete, move.
- *Exit:* each action round-trips to exactly one commit; an edit made in the CLI appears live in the open panel.

**P15 — UI runs + proposal review**
- The Decompose action (with optional guidance), the node's run list, a live transcript (assistant text and tool calls rendered from stream-json), cancel, and continue (resume).
- Proposal review: ghost nodes and edges overlaid on the level view, with inline editing of title, kind, intent and acceptance, and removal of children or edges, then Accept or Reject.
- *Exit (= M4):* a feature is planned entirely in the UI, from a new node → decompose → edit → accept → drill in → decompose again.

#### M5: playback

**P16 — Frames index + history API** (§5)
- A `frames` table built in one `git log --reverse` pass with trailers. It is incremental via `last_indexed_sha`, and a history rewrite triggers a rebuild. WS `index.progress` reports progress on large rebuilds.
- A lazy blob loader with an LRU of parsed graphs; `GET /api/frames`; `GET /api/graph?at=<sha>`; per-node body history.
- *Exit:* the frame count equals the number of plan commits; `?at=` returns the historical level view; after a rewrite, the frames are rebuilt.

**P17 — Playback scrubber + delivery overlay**
- A timeline scrubber (step, play, labels showing each frame's op and actor). Scrubbing puts the view in read-only mode.
- Code-repo scan for `APM-Node` trailers, merged into the timeline by timestamp and shown as markers on nodes.
- *Exit (= M5):* the APM plan (from P09) replays from its first commit.

#### M6–M7: delivery

**P18 — `apm-implementer`** (§7.4)
- The `apm-implementer` agent and the `apm-deliver` skill (branch naming, the `APM-Node` trailer, a PR body template).
- The runner starts it in a worktree (`-w apm/<handle>-<slug>`) with scope `node:<id>` and the `allowed_tools` from `apm.yaml`. The **server** refuses to start it on a node that isn't a ready leaf.
- A PR is opened through `gh` when it's available. Delivery is attached, and the node is set to `in_progress`.
- The UI gains an Implement button on ready leaves and shows the delivery.
- *Exit (= M6):* a ready leaf becomes a PR whose commits carry `APM-Node` and whose node shows the delivery.

**P19 — `apm-reconciler`**
- The `apm-reconciler` agent (scope `project`). It reads `APM-Node` trailers and PR state, then updates status.
- *Decides:* how much of this is deterministic code (a trailer scan plus `gh pr view`) and how much is left to the agent. Recommendation: do the deterministic work in an `apm reconcile` command, and keep the agent for ambiguous cases.
- Triggers: run manually, and once when `apm serve` starts.
- *Exit (= M7):* merging a PR moves its node to `done` with no manual step.

**P20 — v1 release**
- *Decides:* open question 4 (the package name) and open question 3 (whether to auto-commit hand edits).
- README, install instructions for the plugin, `npx` install, and an npm publish workflow.
- *Exit:* `npx <pkg> init` works on a clean machine.

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
4. **Package name.** `apm` is taken on npm (Atom's old package manager). Choose a scoped name or a new name before publishing.
