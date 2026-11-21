// The actions panel: every way to change the graph directly, for whatever is selected. One node,
// several nodes, or (with nothing selected) the plan as a whole.

import type { NodeDetail, NodeView, Status } from "./types";
import { STATUS_LABEL, cx, stackedName } from "./util";

export interface ActionHandlers {
  open: (id: string) => void;
  add: (parent: string | null) => void;
  rename: () => void;
  setStatus: (ids: string[], status: Status) => void;
  toggleKind: (node: NodeView) => void;
  move: (id: string) => void;
  link: (id: string) => void;
  merge: (ids: string[]) => void;
  remove: (ids: string[]) => void;
  runAgent: (agent: string, id: string) => void;
  check: () => void;
  reconcile: () => void;
  commitEdits: () => void;
  exportArchive: () => void;
  clear: () => void;
}

const STATUSES: Status[] = ["todo", "in_progress", "done", "dropped"];

export function Actions({
  selected,
  running,
  handlers,
  lookup,
  detail,
}: {
  selected: NodeView[];
  detail: NodeDetail | null;
  running: Set<string>;
  handlers: ActionHandlers;
  lookup: (id: string) => NodeView | undefined;
}) {
  const one = selected.length === 1 ? selected[0]! : null;
  const ids = selected.map((n) => n.id);
  const sameStatus = selected.length > 0 && selected.every((n) => n.status === selected[0]!.status) ? selected[0]!.status : null;

  const heading = one ? one.title : selected.length ? `${selected.length} nodes selected` : "The whole plan";
  // The server says whether a task is under review and why Implement can't start; the UI follows it.
  const mine = one && detail?.id === one.id ? detail : null;
  const locked = selected.some((n) => n.id === detail?.id && detail.under_review);
  const canImplement = one !== null && one.children === 0 && (one.ready || one.status === "in_progress");
  const implementHint = !one
    ? ""
    : locked
      ? "Under review: wait for its review to finish"
      : mine?.implement_blocker
        ? `Not yet: ${mine.implement_blocker}`
    : one.children > 0
      ? "Only a node with nothing inside it can be built directly"
      : one.blocked
        ? "Blocked: what it waits on isn’t done yet"
        : one.status === "todo" && one.stack_top
          ? `Stacked on ${stackedName(one)}: Claude starts from that branch and opens its pull request against it`
        : one.status === "done" || one.status === "dropped"
          ? `This node is ${STATUS_LABEL[one.status].toLowerCase()}`
          : "Claude builds this in its own worktree and opens a pull request";
  const busy = one !== null && (running.has(one.id) || locked);

  // A feature is reviewed through its review step; the step itself shows the same button.
  const isStep = one?.kind === "review";
  const step = one ? (isStep ? one : one.review_step ? lookup(one.review_step) : undefined) : undefined;
  const reviewWaiting = step ? step.waiting_on.filter((id) => !step.stacked_on.some((s) => s.id === id)) : [];
  const canReview =
    step !== undefined && reviewWaiting.length === 0 && (step.status === "todo" || step.status === "in_progress");
  const reviewHint = !step
    ? ""
    : step.status === "done"
      ? "Reviewed, and the feature has landed"
      : step.status === "dropped"
        ? "The review step is dropped"
        : reviewWaiting.length
          ? `Waits until every task is done (${reviewWaiting.length} to go)`
          : "Claude checks the feature branch against the feature's description, criterion by criterion";
  const reviewBusy = step !== undefined && running.has(step.id);

  return (
    <section className="actions" aria-label="Actions">
      <div className="actions-head">
        <span className="label">Actions</span>
        <span className="actions-target" title={heading}>
          {heading}
        </span>
        {selected.length > 0 && (
          <button className="link" onClick={handlers.clear}>
            Clear
          </button>
        )}
      </div>

      {selected.length === 0 && (
        <div className="action-row">
          <button onClick={() => handlers.add(null)}>Add node</button>
          <button onClick={handlers.check} title="Look for broken rules and warnings">
            Check plan
          </button>
          <button onClick={handlers.reconcile} title="Mark nodes done whose work is on the default branch, and report stacked PRs to retarget">
            Reconcile
          </button>
          <button onClick={handlers.commitEdits} title="Commit edits you made by hand in the .apm folder">
            Commit hand edits
          </button>
          <button onClick={handlers.exportArchive} title="Download the plan and its history as one .apm.zip file">
            Export archive…
          </button>
        </div>
      )}

      {selected.length > 0 && (
        <>
          <div className="action-row">
            <span className="row-label">Status</span>
            <div className="segmented" role="group" aria-label="Status">
              {STATUSES.map((status) => (
                <button
                  key={status}
                  className={cx(sameStatus === status && "on")}
                  aria-pressed={sameStatus === status}
                  onClick={() => handlers.setStatus(ids, status)}
                >
                  {STATUS_LABEL[status]}
                </button>
              ))}
            </div>
          </div>
          <div className="action-row">
            <span className="row-label">Graph</span>
            {one && isStep && (
              <>
                <button onClick={() => handlers.add(one.parent)} title="Add a task to this feature: the review waits on it too">
                  Add task
                </button>
                <button onClick={handlers.rename}>Rename</button>
                <span className="row-hint">APM keeps a review step waiting on every task. Set it to Dropped to skip the review.</span>
              </>
            )}
            {one && !isStep && (
              <>
                <button onClick={() => handlers.open(one.id)} title="Show what is inside this node">
                  Open
                </button>
                <button onClick={() => handlers.add(one.id)}>Add child</button>
                <button onClick={() => handlers.add(one.parent)} title="Add a node next to this one">
                  Add sibling
                </button>
                <button onClick={handlers.rename} disabled={locked}>
                  Rename
                </button>
                <button onClick={() => handlers.toggleKind(one)} title="Work is something to build; a decision is a choice to make">
                  Make {one.kind === "work" ? "decision" : "work"}
                </button>
                <button onClick={() => handlers.move(one.id)} disabled={locked} title="Put it inside another node">
                  Move
                </button>
                <button onClick={() => handlers.link(one.id)} title="Say what this node needs first">
                  Link
                </button>
              </>
            )}
            {!isStep && (
              <>
                <button
                  onClick={() => handlers.merge(ids)}
                  disabled={selected.length < 2 || locked}
                  title={
                    selected.length < 2
                      ? "Select two or more nodes first: shift-click, or ⌘/ctrl-click"
                      : "Fold the selected nodes into one"
                  }
                >
                  Merge
                </button>
                <button className="danger" onClick={() => handlers.remove(ids)} disabled={locked}>
                  Delete
                </button>
              </>
            )}
          </div>
          {one && (
            <div className="action-row">
              <span className="row-label">Claude</span>
              {!isStep && (
                <>
                  <button
                    onClick={() => handlers.runAgent("apm-decomposer", one.id)}
                    disabled={busy}
                    title="Ask Claude to break this node into children. You review the proposal first."
                  >
                    Decompose
                  </button>
                  {one.children === 0 && one.status === "todo" && mine?.review && (
                    <button
                      onClick={() => handlers.runAgent("apm-task-reviewer", one.id)}
                      disabled={busy}
                      title="Claude checks that this task is small enough: at most about 50 lines of real code"
                    >
                      Review task
                    </button>
                  )}
                  {one.children === 0 && (
                    <button
                      onClick={() => handlers.runAgent("apm-implementer", one.id)}
                      disabled={!canImplement || busy || !!mine?.implement_blocker}
                      title={implementHint}
                    >
                      Implement
                    </button>
                  )}
                </>
              )}
              {step && (
                <button
                  className={cx(canReview && isStep && "primary")}
                  onClick={() => handlers.runAgent("apm-feature-reviewer", step.id)}
                  disabled={!canReview || reviewBusy}
                  title={reviewHint}
                >
                  Review feature
                </button>
              )}
              <span className="row-hint">or just say what you want below</span>
            </div>
          )}
        </>
      )}
    </section>
  );
}
