---
name: apm-decompose
description: How to break an APM node into children well, covering how many, how to slice, dependencies and acceptance criteria. Use when decomposing a node, reviewing a proposed breakdown, or adding child nodes to a plan.
---

# Breaking a node down

The goal: someone can pick up any child and finish it without reading its siblings.

## Before you start

1. Read the node (`apm_get_node`) and its surroundings (`apm_get_graph`), including what it
   depends on and what depends on it.
2. Search for work that already exists (`apm_search`). Do not propose a node that duplicates one
   elsewhere in the plan: depend on it instead, and say so in `warnings`.
3. Read the code the node is about. A breakdown that ignores what already exists is a guess.

## What a good breakdown looks like

- **3 to 7 children.** Fewer means the node was already small enough. More means you are two
  levels deep: group them.
- **Together, the children are the parent.** Finishing all of them finishes it, and nothing in
  them goes beyond it.
- **No overlap.** Each piece of work belongs to exactly one child.
- **One level only.** Do not break the children down further. That happens later, on request.
- **Sliced so each child can be verified alone.** Prefer slices that end in something you can run
  or test over slices by layer ("all the models", "all the views").
- **Small leaves.** A child meant to be built directly should fit in one pull request. If it
  won't, it is not a leaf yet. Leave it as a parent to be broken down later.

## The review step

APM gives every node with children a **review step** of its own: one more child, of kind
`review`, that depends on all the others. Once they are done, the feature reviewer checks the
feature branch against the parent's description. So:

- Never propose a "review", "check everything" or "final verification" child: APM adds it.
- Write the parent's acceptance criteria so they can be checked against the finished code: the
  review holds the feature to them.

## Dependencies

- Add a dependency only where one child truly cannot start until another is done. Shared context
  is not a dependency.
- Prefer edges between siblings (`depends_on_keys`).
- For a dependency on existing work elsewhere, use its handle (`depends_on_ids`), and point at the
  most specific node that is true, never at an ancestor of the node being decomposed.
- Never create a cycle. If A needs part of B and B needs part of A, split one of them.
- Leave room for parallel work: a chain of seven is a warning sign.

## Each child

- **Title:** an outcome, in a few words. "Parse graph.ndjson", not "Parsing".
- **Kind:** `decision` when the work is choosing (which library, which format). The children that
  depend on the choice should depend on that node.
- **Intent:** one or two sentences. Why it exists and what is true when it is done.
- **Acceptance:** 2 to 5 statements that can be checked. "Rejects a line with an unknown key and
  reports its line number", not "Handles errors well".

## Say what you are unsure about

Use `warnings` for anything a person should decide: an assumption you made, a near-duplicate you
found, a risk, a part of the node you could not place.
