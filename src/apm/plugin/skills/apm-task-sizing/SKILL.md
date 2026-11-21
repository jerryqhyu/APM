---
name: apm-task-sizing
description: How to size an APM task, covering what counts as a real line of code, the 50-line limit, how to estimate, and when the verdict is pass, too_big or unclear. Use when reviewing a task before it is built, or when writing tasks that must stay small.
---

# Sizing a task

A task must be small enough that a person can read and understand its code in about five
minutes. The rule: **at most 50 real lines of source code** per task.

## What counts as a real line

A real line is a line of source code that a reviewer has to read and reason about. These do
**not** count:

- blank lines;
- comments and docstrings;
- imports;
- tests (test files, fixtures, test helpers);
- docs (README, user guide, `plans.md`, other prose);
- skill and agent markdown (`SKILL.md`, `agents/*.md`).

Everything else counts: logic, signatures, data structures, configuration code, schemas written
as code, UI components and styles. A line reformatted across three lines by the formatter counts
as three.

What doesn't count may be extra, but it should be proportionate: a 30-line change with 400 lines
of tests or docs is still hard to review. Mention it in the summary if it is out of proportion.

## How to estimate

1. Read the task and its acceptance criteria. Each criterion is something the code must do.
2. Read the code it touches. Find where each criterion lands: which file, which function, and
   what already exists that the task can reuse.
3. Sketch the change file by file: new functions, changed branches, new fields, new routes or
   commands. Give each part a line count, rounded to the nearest five.
4. Add them up. Count the real lines only, by the rule above.

Estimate the code a careful author in this repository would write, following its conventions,
not the shortest possible code. Count changed lines as well as added ones.

## Verdicts

- **pass**: the change is clear and the estimate is at most about 50 real lines.
- **too_big**: the estimate is clearly over 50. Propose a split into two or more tasks, each
  within the limit, that together do exactly what the task asked. Slice so each piece can be
  tested on its own (see the `apm-decompose` skill), not by layer.
- **unclear**: you can't estimate, because the task doesn't say what to build: no intent, no
  acceptance criteria, or a choice it leaves open that changes the size. List what is missing.

Judge the reader, not the number to the line: 55 lines of plain code is a pass, while 45 lines
that are really two unrelated changes may be worth splitting. A task over 100 real lines is
always `too_big`.

## Worked examples

### pass: about 20 lines

> **Add `--limit` to `apm ready`.** Intent: show at most N ready nodes. Criteria: `apm ready
> --limit 3` prints at most three nodes; without it, output is unchanged; `--limit 0` is an
> error.

Reading `cli.py`, `ready` already builds a list and prints it. The change: a typer option (about
3 lines), a check that rejects 0 (about 3), slicing the list before printing (about 2), and
the same for the JSON output (about 2). With the formatter's line breaks, about **10 to 20 real
lines**. Tests are extra. Verdict: `pass`.

### too_big: more than 100 lines

> **Task review with a UI.** Intent: review every task's size automatically and show the result.
> Criteria: a reviewer agent and its output schema; a store for review results; a queue that
> reviews tasks when they change; a lock while a review runs; a badge and a Review button in the
> UI.

Sketch: the agent registration and schema (about 30), the result store (about 40), the debounced
queue (about 50), the lock checked by every mutation (about 30), the UI badge and button (about
40). About **190 real lines**: well over 100. Verdict: `too_big`, split along those parts:

1. `schema`: register the reviewer with its verdict schema and parser.
2. `store`: store review results keyed by the task's content (depends on `schema`).
3. `queue`: queue a review when a task changes, debounced (depends on `store`).
4. `lock`: refuse changes to a task while it is under review.
5. `ui`: show the review state and add the Review button (depends on `store` and `lock`).

Each has its own intent and testable criteria, and each fits within 50 lines.

### unclear: a one-line vague title

> **Improve performance.** (No intent, no criteria.)

There is nothing to estimate: it doesn't say what is slow, how it is measured, or what counts as
done. Verdict: `unclear`, with `missing`:

- Which operation is slow (a command, a page, an API call)?
- How slow it is now, and how it was measured.
- What target counts as done.
