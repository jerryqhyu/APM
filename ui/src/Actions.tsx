// The actions panel: every way to change the graph directly, for whatever is selected. One node,
// several nodes, or (with nothing selected) the plan as a whole.

import type { NodeView, Status } from "./types";
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
}: {
  selected: NodeView[];
  running: Set<string>;
  handlers: ActionHandlers;
}) {
  const one = selected.length === 1 ? selected[0]! : null;
  const ids = selected.map((n) => n.id);
  const sameStatus = selected.length > 0 && selected.every((n) => n.status === selected[0]!.status) ? selected[0]!.status : null;

  const heading = one ? one.title : selected.length ? `${selected.length} nodes selected` : "The whole plan";
  const canImplement = one !== null && one.children === 0 && (one.ready || one.status === "in_progress");
  const implementHint = !one
    ? ""
    : one.children > 0
      ? "Only a node with nothing inside it can be built directly"
      : one.blocked
        ? "Blocked: what it waits on isn’t done yet"
        : one.status === "todo" && one.stack_top
          ? `Stacked on ${stackedName(one)}: Claude starts from that branch and opens its pull request against it`
        : one.status === "done" || one.status === "dropped"
          ? `This node is ${STATUS_LABEL[one.status].toLowerCase()}`
          : "Claude builds this in its own worktree and opens a pull request";
  const busy = one !== null && running.has(one.id);

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
            {one && (
              <>
                <button onClick={() => handlers.open(one.id)} title="Show what is inside this node">
                  Open
                </button>
                <button onClick={() => handlers.add(one.id)}>Add child</button>
                <button onClick={() => handlers.add(one.parent)} title="Add a node next to this one">
                  Add sibling
                </button>
                <button onClick={handlers.rename}>Rename</button>
                <button onClick={() => handlers.toggleKind(one)} title="Work is something to build; a decision is a choice to make">
                  Make {one.kind === "work" ? "decision" : "work"}
                </button>
                <button onClick={() => handlers.move(one.id)} title="Put it inside another node">
                  Move
                </button>
                <button onClick={() => handlers.link(one.id)} title="Say what this node needs first">
                  Link
                </button>
              </>
            )}
            <button
              onClick={() => handlers.merge(ids)}
              disabled={selected.length < 2}
              title={
                selected.length < 2
                  ? "Select two or more nodes first: shift-click, or ⌘/ctrl-click"
                  : "Fold the selected nodes into one"
              }
            >
              Merge
            </button>
            <button className="danger" onClick={() => handlers.remove(ids)}>
              Delete
            </button>
          </div>
          {one && (
            <div className="action-row">
              <span className="row-label">Claude</span>
              <button
                onClick={() => handlers.runAgent("apm-decomposer", one.id)}
                disabled={busy}
                title="Ask Claude to break this node into children. You review the proposal first."
              >
                Decompose
              </button>
              <button
                onClick={() => handlers.runAgent("apm-implementer", one.id)}
                disabled={!canImplement || busy}
                title={implementHint}
              >
                Implement
              </button>
              <span className="row-hint">or just say what you want below</span>
            </div>
          )}
        </>
      )}
    </section>
  );
}
