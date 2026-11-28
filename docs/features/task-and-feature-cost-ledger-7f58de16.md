# Task and feature cost ledger

## What it is and why it exists

APM runs several kinds of Claude agents on each node of a plan: the task reviewer, the implementer, the critic, PR reviewers, fixes and retries. Before this feature, each run had a `cost_usd`, but nothing added those costs up by task or feature, and nothing kept a fixed figure once the work was done.

This feature gives every task and feature a running cost, as a total and split by agent, and freezes that cost when the work merges. The request came from the user while planning the parent feature, "Cost view and feature budgets" (28c771ed):

> "I want each task to come with a singular session that tracks the session cost. When the PR is merged we should crystalize the cost. We should also be tracking ongoing cost of a feature, and when the feature is merged we should crystalize the cost on the feature level."

The feature's two acceptance criteria:

- A task's cost covers every run on it, split by agent.
- A merged task's or merged feature's cost no longer changes when later runs happen.

This feature only builds the ledger. The views, `apm costs` and the budget cap belong to the parent feature (28c771ed) and will read these numbers. Nothing outside `reconcile.py` calls the new code yet.

## Decisions

**Every agent's runs count toward a node's cost, split by agent.** Claude asked whether only the implementer's session should count or every agent's. The user answered: "All cost involved, and if we can breakdown by agent type that would be even better." This is why `Store.node_cost` groups by agent and leaves no run kind out.

**Frozen costs live in the `.apm` folder, next to the plan.** Claude offered two places: the plan files, versioned in git, or the local store next to `runs`. The user chose the first: "The cost live in the apm database similar to where we store features and tasks (the .apm folder)." Claude then put frozen costs in `.apm/costs.ndjson`, beside `graph.ndjson`. Live costs are still worked out from the local `apm.db`, which isn't committed. Claude told the user this and offered to keep frozen costs in `apm.db` instead. The user didn't take up that offer.

**"One session per task" became one cost record per task, not one shared Claude conversation.** This was Claude's reading, given to the user during planning: making every agent continue one conversation "would be a much bigger change, and you asked for all costs anyway." The user went ahead with the breakdown without objecting.

**Eager builds count toward the feature only.** Claude also pointed this out during planning: eager builds run on the feature node, so their cost isn't divided among the tasks. It offered to have the split step divide it but didn't plan that. The user didn't ask for it.

**Small tasks.** The user asked Claude to "be mindful of my task size when you split." Each of the six tasks came out at about 15–20 lines of source code, against a 50-line limit.

**The breakdown itself.** Claude split the work into six tasks during the 2026-11-27 03:30 planning chat, and the user accepted that breakdown. Later the user told Claude to "Build the cost tasks." The record gives no other reasons for how the tasks were sliced.

**Choices made inside tasks.** Implementers and task reviewers made these choices. The user didn't decide them, but they shape the feature:

- **Cumulative cost.** Claude's `total_cost_usd` turned out to be cumulative across a resumed session, so each run keeps only what it added (#61).
- **Frozen task records name the PR URL; frozen feature records name the branch** (#66, #69).
- **No PR, no task freeze.** A task found done without a merged PR (a squash found by patch id, or a direct push) is not frozen (#66).
- **Only top-level features freeze.** Task 7037cbd2 was scoped as "when its branch merges into main". So a nested feature that merges into its parent feature's branch is not frozen on its own; its cost is counted inside its parent's record (#69).

## How it is put together

There are three layers: per-run costs in the local database, live sums over the plan, and frozen records next to the plan.

1. **Per-run cost** (`src/apm/core/store.py`, `Store.finish_run`). The runner passes Claude's reported `total_cost_usd` when a run finishes. `finish_run` subtracts the costs already stored for earlier runs of the same session (same `session_id`, started no later than this run). If the reported figure is lower than that sum, it assumes Claude restarted its count and keeps the figure whole.
2. **A node's own cost** (`Store.node_cost`). One SQL query, `GROUP BY agent` over `runs.node_id`. It returns `NodeCost(total, by_agent)`. Runs with no cost yet count as 0.
3. **A subtree's cost** (`live_cost` in `src/apm/core/costs.py`). If the node has a frozen record, it returns that record and stops there. Otherwise it adds the node's own runs to the `live_cost` of each direct child. Walking only direct children means every node counts exactly once. A frozen sub-feature counts by its frozen figure, which already includes its subtree. The caller reads the frozen records once and passes them in.
4. **Frozen records** (`costs.py`: `CostRecord`, `read_costs`, `freeze_cost`). `.apm/costs.ndjson` holds one JSON line per node, sorted by node id, with fields `node_id`, `total`, `by_agent`, `frozen_at` and `merged`. `freeze_cost` takes the `.apm` write lock, refuses a second record for the same node with `CostFrozenError`, and writes the file atomically, as `graph.ndjson` is written. A missing file reads as no records.
5. **The trigger** (`src/apm/core/reconcile.py`). Reconcile decides which nodes have landed:
   - **Tasks.** When it marks a task done because its PR merged, `_Judge` sets `merged_pr`. `_freeze_task_cost` then writes the task's `node_cost`, with `merged` set to the PR URL. This covers a PR merged into the home branch, and a PR merged into a stack branch that has since landed there.
   - **Features.** When it marks a top-level feature's review step done (`graph.feature_of(node_id) is None`), `freeze_feature_cost` writes the feature's `live_cost`, with `merged` set to the feature branch.
   - **Repeats.** Both helpers return early if a record already exists, so reconciling again never raises an error or rewrites a record.

`costs.ndjson` is written but not committed to the plan repo by this code. Task ddfbfb9b left that decision to the freezing tasks, and neither freezing task commits it.

## The work

**Store frozen costs in .apm (ddfbfb9b, #60).** Added `src/apm/core/costs.py` with `CostRecord`, `read_costs` and `freeze_cost`, reusing the atomic-write helpers in `core/files.py`. Because `freeze_cost` takes the write lock itself, it must not be called while that lock is already held (for example inside `mutate`). The implementer also created the two feature branches, at main's tip, so this PR had a branch to target.

**Make each run's cost its own, not the session's (8cf5074e, #61).** This task first had to find out whether Claude's `total_cost_usd` is cumulative on a resumed session. It is: a resumed run that failed after 384 ms with 0 turns and 0 tokens reported $0.4907, the previous run's exact total. `Store.finish_run` now stores the reported total minus the earlier runs' stored costs. The implementer called the restart detection a heuristic: "a fresh restart that happens to land above the earlier sum would be undercounted."

**Live cost of a task, by agent (480bfe29, #64).** Added the `NodeCost` dataclass and `Store.node_cost` in `store.py`. Only the node's own runs count. Rolling costs up through the plan is left to the next task.

**Live cost of a feature, by agent (0f5be613, #65).** Added `live_cost` to `costs.py`, the recursive walk described above. Feature-review children are counted like any other child. CI failed once on an unrelated flaky test (`tests/mcp/test_server.py::test_check_also_checks_apm_yaml`): a random all-digit node handle is read by YAML as a number. The failed job was rerun and passed.

**Freeze a task's cost when its PR merges (e118395d, #66).** Added `merged_pr` to `_Judge` and `_freeze_task_cost` to `reconcile.py`. On request from the user ("a test failed can you fix and push to the pr"), this PR also fixed a race in `tests/runner/test_auto_entry_points.py`, which failed only on macOS. The server fixture now depends on `reviewed`, so the task's review is saved before the server's startup scan can begin a review of its own.

**Freeze a feature's cost when its branch merges into main (7037cbd2, #69).** Added `freeze_feature_cost` and its call in `reconcile`. It conflicted with #66 in the same lines of `reconcile.py` and its tests. The user asked for a rebase. Claude merged the feature branch in instead (commit `a6e83f8`), because a rebase would have needed a force-push, which it isn't allowed to do. Both behaviours and both tests were kept.

## How it was tested

New tests, all deterministic and offline:

- **Per-run cost** (`tests/core/test_store.py`):
  - `test_a_resumed_run_keeps_only_the_cost_it_added`: a $1 run, then a cumulative $3, are stored as $1 and $2.
  - `test_fresh_runs_keep_the_cost_they_report`.
- **A node's cost** (`tests/core/test_store.py`):
  - `test_a_node_costs_the_sum_of_its_runs_by_agent`.
  - `test_runs_without_a_cost_yet_count_as_nothing`.
- **Frozen records** (`tests/core/test_costs.py`):
  - `test_a_frozen_cost_round_trips_every_field`.
  - `test_freezing_a_node_twice_is_refused`.
  - `test_a_missing_file_reads_as_no_records`.
- **Feature roll-up** (`tests/core/test_costs.py`):
  - `test_a_feature_totals_its_runs_and_its_frozen_and_live_tasks`.
  - `test_a_sub_feature_counts_once_in_its_parent`.
- **Freezing in reconcile** (`tests/core/test_reconcile.py`):
  - `test_a_merged_pr_freezes_the_tasks_cost_once`: covers reconciling twice and a run after the merge.
  - `test_a_node_done_without_a_merged_pr_freezes_nothing`.
  - `test_a_feature_whose_branch_merged_into_main_has_its_cost_frozen`.

Each PR ran ruff and the full pytest suite (716–720 tests), and the later ones also ran ty. All passed except one test, `test_serve_prints_a_url_that_works`, which fails locally because macOS hid the virtual environment's `apm.pth` file ("No module named apm"). That is a local setup problem, not a code problem. The feature reviewer ran the 42 tests covering these changes, and all passed.

The feature review ran three times, all on commit `8e31f6c`, and found both acceptance criteria met each time. The third run couldn't use git, because the review worktree's `.git` pointed to a path on another machine. It confirmed the code unchanged by reading it.

## Left for later

The feature review raised these as minor issues. None of them was fixed, and no follow-up tasks were proposed.

- **Tasks that land without a merged PR are never frozen.** This covers a squash found by patch id and a direct push. Their cost keeps changing if more runs happen. This was deliberate in #66, and a test checks it.
- **Nested features are never frozen on their own.** A feature whose branch merges into its parent feature's branch stays live until the outer feature freezes. This follows task 7037cbd2's "into main" wording, but the feature's own intent says a cost freezes "when its work merges". The record and the code differ here.
- **Features finished in `feature_review.conclude`'s `landed` path aren't frozen.** When a feature review passes on a branch that is already the feature's destination, `src/apm/core/feature_review.py` (about lines 505–521) marks the feature done without calling `freeze_feature_cost`. The reviewer called this the easiest gap to close.
- **Eager-build and splitter runs are counted on the feature, not its tasks.** The feature's total is right, but the tasks' own costs leave the build out. This was flagged during planning and left as is.
- **A run's cost can still be wiped.** #61 noted that in `Runner._execute`, a run that succeeds without a structured answer calls `_finish` a second time without `cost_usd`, and this sets the run's stored cost to NULL. The note suggested fixing it in 480bfe29 or as a follow-up. In the code on this branch it is still not fixed: `src/apm/runner/runner.py` line 932 makes that second call, and `finish_run` writes `cost_usd = NULL` when no cost is passed.
- **`costs.ndjson` is not committed to the plan repo.** It is written next to `graph.ndjson`, but nothing here commits it. ddfbfb9b left that to the freezing tasks, and neither did it.
- **The restart heuristic can undercount.** In `finish_run`, a Claude restart whose new count happens to exceed the earlier runs' sum would be undercounted (#61).
- **A flaky test remains outside this feature.** `tests/mcp/test_server.py::test_check_also_checks_apm_yaml` fails on all-digit handles. The suggested fix is to quote the handle in that test, as a separate small task.

---

_Written by APM's feature reporter from the plan's record of feature `7f58de16`, at standard detail, on 2026-11-28._
