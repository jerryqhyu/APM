---
expected: pass
title: Add --limit to apm ready
---

## Intent

`apm ready` lists every ready node, which is long on a big plan. Add `--limit N` to show at most
N of them, in the order it already uses.

## Acceptance criteria

- [ ] `apm ready --limit 3` prints at most three nodes, the first three it would print today.
- [ ] `apm ready --limit 3 --json` returns at most three nodes.
- [ ] Without `--limit`, the output is unchanged.
- [ ] `apm ready --limit 0` fails with an error saying the limit must be at least 1.
