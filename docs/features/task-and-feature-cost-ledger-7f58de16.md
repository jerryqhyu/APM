# Task and feature cost ledger

## What it is and why it exists

This feature gives every task and feature in an APM plan a running cost: a total in USD, split by agent type (implementer, reviewer, critic and so on). When the work merges, that cost is frozen. The feature description states the goal: "Every task and feature has a running cost (total and by agent) that is frozen when its work merges. All agents that work on a node count toward it."

It came out of a conversation on the parent feature, "Cost view and feature budgets" (28c771ed). There the user asked for "each task to come with a singular session that tracks the session cost. When the PR is merged we should crystalize the cost", and the same for features when the feature branch merges. The parent feature's views (`apm costs`, totals per agent and per day) and its budget cap are meant to read from the numbers this feature produces. Those are not part of this branch.

The feature has two acceptance criteria:

- A task's cost covers every run on it (reviewer, implementer, critic, PR review, fixes, retries), split by agent.
- A merged task's or feature's cost no longer changes when later runs happen.

The feature review (turn 1, at `8e31f6c`) found both criteria met on the main path. It also listed four minor gaps, which are described under "Left for later".

## Decisions

**Every agent's cost counts toward a node, split by agent type.** Claude asked whether a task's "singular session" meant only the implementer's cost, or every agent that works on the task. The user answered: "All cost involved, and if we can breakdown by agent type that would be even better." That answer is why `Store.node_cost` groups by `agent`, and why `live_cost` keeps the per-agent breakdown.

**A cost record per task instead of one Claude conversation per task.** The user had asked for "a singular session" per task. Claude's plan replaced that with a cost record per task. Its reason: "Making every agent continue a single conversation would be a much bigger change, and you asked for all costs anyway." The user then asked Claude to start the cost tasks, so this reading went ahead. The user did not otherwise comment on it.

**Frozen costs live in `.apm`, next to the plan.** Claude asked whether frozen costs should be versioned with the plan files or kept in the local store. The user said: "The cost live in the apm database similar to where we store features and tasks (the .apm folder)." Claude read this as a file next to `.apm/graph.ndjson`, which became `.apm/costs.ndjson`. Live costs are still worked out from the local `apm.db`, which is not committed. Claude said so: "Tell me if you wanted frozen costs in `apm.db` instead." No reply to that is recorded. One gap: neither `costs.py` nor the two freezing tasks commit `costs.ndjson` to the plan repo. The `ddfbfb9b` notes left that choice to the freezing tasks, and they did not make it either (see "Left for later").

**Eager builds count toward the feature only.** Claude noted that eager builds run on the feature, not on its tasks, "so their cost counts toward the feature only". It offered to have the split step divide that cost among the tasks, but did not plan it. No response from the user is recorded.

**Keep tasks small.** The user asked Claude to "be mindful of my task size when you split." The work was split into six tasks. Each task review estimated 10 to 45 real lines against the 50-line limit.

**Merging instead of rebasing for the `7037cbd2` conflict.** The user asked Claude to "rebase fix and push". Claude merged the feature branch into the task branch instead (commit `a6e83f8`), because a rebase would need a force-push, which it is not allowed to do. Claude kept both sides of the conflict.

The design choices inside each task (for example, not freezing tasks that merged without a PR, and not freezing nested features on their own) were made by the agents that built them. They are recorded in the task notes, not decided by the user. They are described below as the agents' choices.

## How it is put together

There are three layers:

1. **Per-run cost, in `src/apm/core/store.py`.** `Store.finish_run` receives the `total_cost_usd` that Claude reports. For a resumed session, that figure is the running total of the whole session. So `finish_run` subtracts the costs already stored for earlier runs on the same session (runs with the same `session_id` that started no later than this one). If the reported figure is lower than that sum, it assumes Claude restarted its count and keeps the figure whole.
2. **Live cost.**
   - `Store.node_cost(node_id)` returns a `NodeCost(total, by_agent)` for one node's own runs. It is a single `GROUP BY agent` query, and a run with no cost yet counts as 0.
   - `live_cost(graph, store, frozen, node_id)` in `src/apm/core/costs.py` works out a whole subtree. If the node has a frozen record, it returns that record and stops. Otherwise it adds the node's own runs to the `live_cost` of each direct child. Because the walk only goes through direct children, each node is counted once. A frozen sub-feature counts by its frozen figure.
3. **Frozen cost, in `src/apm/core/costs.py`.**
   - Each node's frozen cost is a `CostRecord(node_id, total, by_agent, frozen_at, merged)`. `merged` holds the PR URL or the branch whose merge froze it.
   - `read_costs` returns every record, or none if the file is missing.
   - `freeze_cost` takes the `.apm` write lock, refuses a second record for the same node (`CostFrozenError`), and rewrites the file atomically. The file is sorted by node id with keys in a fixed order, like `graph.ndjson`.

Freezing is triggered from `reconcile()` in `src/apm/core/reconcile.py`:

- **Tasks.** `_Judge` now remembers `merged_pr` when a merged pull request is the reason a node is done. That covers a PR merged into the home branch, and a PR merged into a stack branch that has since landed there. After `set_status(DONE)`, `_freeze_task_cost` writes the task's `Store.node_cost` with the PR URL. It skips the write if the task already has a record.
- **Features.** When a feature's review step is marked done and the feature has no enclosing feature (`graph.feature_of(...) is None`), `freeze_feature_cost` writes the feature's `live_cost`, with the feature branch's name in `merged`. It also does nothing if the feature is already frozen.

To sum up the flow: runs write their own cost to `apm.db`; `node_cost` and `live_cost` read it; reconcile turns live figures into frozen records in `.apm/costs.ndjson`; and `live_cost` prefers those frozen records from then on.

## The work

**Make each run's cost its own, not the session's (8cf5074e, #61).** This task first found out whether Claude's `total_cost_usd` is cumulative on a resumed session. It is. A resumed run that failed after 384 ms, with no turns, reported $0.4907, the previous run's exact total. `finish_run` now stores only what each run added. The notes call this a heuristic: "a fresh restart that happens to land above the earlier sum would be undercounted." The task also flagged a bug outside its scope: in the runner, a run that succeeds without a structured answer calls `_finish` a second time without `cost_usd`, which sets the stored cost to NULL. That bug is still present (see "Left for later").

**Live cost of a task, by agent (480bfe29, #64).** This task added the `NodeCost` dataclass and `Store.node_cost`. A node with no runs gives `total=0, by_agent={}`. An agent whose runs have no cost yet shows as 0.0.

**Store frozen costs in .apm (ddfbfb9b, #60).** This task added `src/apm/core/costs.py` with `CostRecord`, `read_costs`, `freeze_cost` and `CostFrozenError`. It reuses `write_file_atomic` and `read_file_if_exists` from `core/files.py`. `freeze_cost` takes the write lock itself, so callers must not already hold it (for example, inside `mutate`). The implementer also created both feature branches from main's tip, so this PR could target this feature's branch.

**Live cost of a feature, by agent (0f5be613, #65).** This task added `live_cost` to `costs.py`. The caller passes in the frozen records, so the file is read once. Review-step children, such as the feature review, count like any other child. The first CI run failed in an unrelated, flaky test (`tests/mcp/test_server.py::test_check_also_checks_apm_yaml`). That test writes a random node handle into `apm.yaml` without quotes, and YAML reads an all-digit handle as a number. Claude reran the job, and it passed. The flaky test itself was not fixed.

**Freeze a task's cost when its PR merges (e118395d, #66).** This task added `merged_pr` to `_Judge` and `_freeze_task_cost` to `reconcile.py`. Nothing is frozen for nodes found done through their commits or patch ids (no PR), or for review steps. When the user reported a failing test on this PR, Claude fixed a race in `tests/runner/test_auto_entry_points.py`. The server fixture now waits for the task's review to be saved before the server starts. Otherwise the server's startup scan could see the task as unreviewed and start a review of its own. The race only showed on the slower macOS runner.

**Freeze a feature's cost when its branch merges into main (7037cbd2, #69).** This task added `freeze_feature_cost`. A nested feature that merges into its outer feature's branch is not frozen on its own, but its cost is included when the outer feature freezes. This PR conflicted with #66 in `reconcile.py` and its tests. Claude resolved it with a merge that keeps both freezes.

## How it was tested

Each task added tests for its acceptance criteria:

- **`tests/core/test_store.py`**
  - `test_a_resumed_run_keeps_only_the_cost_it_added`: a fake two-run session reporting $1 and then a cumulative $3 stores $1 and $2.
  - `test_fresh_runs_keep_the_cost_they_report`
  - `test_a_node_costs_the_sum_of_its_runs_by_agent`
  - `test_runs_without_a_cost_yet_count_as_nothing`
- **`tests/core/test_costs.py`**
  - The record round-trips all its fields.
  - A second freeze of the same node is refused.
  - A missing file reads as no records.
  - A feature totals its own runs plus a frozen task and a live task.
  - A sub-feature counts once in its parent.
- **`tests/core/test_reconcile.py`**
  - `test_a_merged_pr_freezes_the_tasks_cost_once`: reconciling twice and adding a run after the merge both leave the record unchanged.
  - `test_a_node_done_without_a_merged_pr_freezes_nothing`
  - `test_a_feature_whose_branch_merged_into_main_has_its_cost_frozen`

The task notes report that the full suite (716 to 720 tests), ruff and ty passed. The exception was `test_serve_prints_a_url_that_works`, which fails on the developer's Mac because macOS hid the venv's `apm.pth` file ("No module named apm"). The notes treat that as a local setup problem. The feature review ran the 42 tests in the touched files, and they passed.

## Left for later

- **The cost-wiping bug is still there.** `src/apm/runner/runner.py` line 932 calls `_finish` a second time, without `cost_usd`, when a run succeeds without a structured answer. #61 flagged this and suggested fixing it in 480bfe29 or as a follow-up. That did not happen. The new `finish_run` still writes `cost_usd = NULL` when no cost is passed, so such a run's cost is lost.
- **A feature done without going through reconcile is not frozen.** When a feature review passes on a branch that is already the feature's home, `src/apm/core/feature_review.py` (around lines 505–521) marks the feature done directly. `freeze_feature_cost` is never called on that path. (Feature review, minor.)
- **Tasks merged without a PR are not frozen.** A task found done through its commits or patch ids keeps a live cost for good. This was intended in e118395d. (Feature review, minor.)
- **Nested features are not frozen on their own.** A nested feature that merges into its parent's branch has no record of its own until the outer feature freezes. Tasks, by contrast, freeze when they merge into a feature branch. 7037cbd2 chose this on purpose. (Feature review, minor.)
- **`costs.ndjson` is neither committed nor git-ignored.** It is written into `.apm`, but it is not added to `.apm/.gitignore`, so it sits as an untracked file in the plan repo. That contradicts the stated intent of keeping frozen costs next to the versioned plan. (Feature review, minor.)
- **The restart heuristic can undercount.** If Claude restarts its count within a session and the new figure lands above the earlier runs' sum, `finish_run` undercounts that run (noted in #61).
- **Eager-build cost is not shared out.** It is not divided among a feature's tasks. Claude offered this, but it was not planned.
- **Two unrelated test fixes are still open.** Quoting the handle in the flaky `tests/mcp/test_server.py::test_check_also_checks_apm_yaml` was suggested as a separate small task. The macOS `apm.pth` problem is a local environment issue.
- **The views and budget cap come later.** `apm costs`, the per-day totals and the budget cap belong to the parent feature (28c771ed) and will build on this ledger.

---

_Written by APM's feature reporter from the plan's record of feature `7f58de16`, at standard detail, on 2026-11-27._
