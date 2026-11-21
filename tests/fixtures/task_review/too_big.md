---
expected: too_big
title: Task review with a UI
---

## Intent

Size every task automatically before it can be built, and show the result in the UI.

## Acceptance criteria

- [ ] A read-only reviewer agent is registered, with a JSON verdict schema (`pass`, `too_big`
  with a split, `unclear` with what is missing) and a strict parser that rejects bad output.
- [ ] Review results are stored per task, keyed by a fingerprint of the task's content, and
  survive a restart.
- [ ] Creating or editing a task queues a review; five edits within two seconds give one review.
- [ ] While a review runs, editing, moving, merging or deleting the task is refused through the
  API, CLI and MCP.
- [ ] Implement is refused unless the latest review passed for the current content.
- [ ] The UI shows each task's review state as a badge and has a Review task button.
