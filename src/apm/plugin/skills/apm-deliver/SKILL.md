---
name: apm-deliver
description: How to deliver an APM leaf node as code, covering branch naming, the APM-Node commit trailer, the pull request and recording the delivery on the node. Use when implementing a node from an APM plan.
---

# Delivering a leaf

## Branch

`apm/<handle>-<short-slug>`, for example `apm/1c0b9a44-stream-output`. One branch per node.

## Commits

Every commit that delivers a node ends with a trailer naming the node's **full id** (from
`apm_get_node`), after a blank line:

```
Stream agent output to the UI

APM-Node: 0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44
```

That trailer is how code is linked back to the plan.

## Pull request

- Title: the node's title.
- Body: the node's intent in a sentence, the acceptance criteria as a checklist (ticked only when
  verified), what was tested and how, and anything left out. End with `APM-Node: <full id>`.
- Open it with `gh pr create` if `gh` is available. If it is not, push the branch and say so.

## Record it on the node

1. `apm_set_status(node, "in_progress")` when you start.
2. `apm_attach_delivery(node, branch=..., commits=[...], pr=...)` when the work is pushed.
3. Tick the acceptance criteria you verified in the node's body (`apm_update_node` with `body`).
   Add anything the next person needs under **Notes**.

Leave the status at `in_progress`. The node becomes `done` when a person marks it, or when the
pull request is merged.

## Stay inside the node

Build what the node's body asks for and nothing else. If you find the node is wrong, too big, or
blocked by something the plan doesn't show, stop and say so in **Notes** instead of widening the
change.
