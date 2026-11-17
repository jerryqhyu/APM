---
name: apm-node-format
description: How an APM plan is structured, including nodes, handles, statuses, dependencies, bodies and the rules the graph must keep. Use when reading or changing an APM plan through the apm_* tools or the `apm` command.
---

# The APM plan

A project is a **nested graph of nodes**. Read it with `apm_get_graph` (outline) and
`apm_get_node` (one node in full).

## Nodes

| Field | Meaning |
|---|---|
| handle | The last 8 hex characters of the node's id. Use it to refer to a node. |
| title | One line. |
| kind | `work` (default) or `decision` (a choice to make, not something to build). |
| status | `todo`, `in_progress`, `done` or `dropped`. |
| parent | At most one. No parent means top level. |
| depends on | Any other nodes, at any level. |
| delivery | Leaves only: where the work landed (branch, commits, PR). |
| body | Markdown, in the sections below. |

An outline line reads:

```
1c0b9a44 [work/todo·blocked] Stream agent output (2/5) → 7d2e0f11
```

- `todo·ready`: nothing is in the way. `todo·blocked`: a dependency isn't done yet.
- `(2/5)`: only on a node with children: 2 of its 5 leaves are done.
- `→ 7d2e0f11`: it depends on that node.

## Body

```markdown
## Intent

Why this exists, and what outcome it's for.

## Acceptance criteria

- [ ] A testable statement.

## Notes
```

## Rules

These are checked on every write. A change that breaks one is refused and nothing is written.

1. A node has one parent, and nesting never loops.
2. Dependencies never form a cycle.
3. A node can't depend on its own ancestor or descendant. Depend on a sibling instead.
4. A dependency must exist.
5. Only a leaf can carry a delivery.

Two more are warnings: a `done` parent with unfinished children, and "entanglement", where two
siblings each contain work the other is waiting for. Entanglement usually means the breakdown
should be reshaped.

## What follows from the rules

- **Blocking is inherited.** If a parent is waiting on something, so is everything inside it.
- **A dropped dependency never unblocks.** It is flagged for a person to resolve.
- **Store an edge where it is true.** If only one child of A needs one child of B, link those two
  children, not A and B. The level above shows the edge automatically.
- **Don't write a status you can compute.** There is no "blocked" status to set.
