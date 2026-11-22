// Eager mode on a feature: its eager build, each task's part of it as a chain of pull requests,
// what changed since the split, and Redo.

import { useState } from "react";
import type { EagerFeature, EagerState, EagerTask, EagerTaskInfo, NodeDetail } from "./types";
import { Spinner } from "./ui";
import { cx, errorText } from "./util";

/** A feature's eager build, as opposed to a task's place in one. */
export function eagerFeature(detail: NodeDetail | null | undefined): EagerFeature | null {
  return detail?.eager && "applies" in detail.eager ? detail.eager : null;
}

/** A task's place in an eager feature. */
export function eagerTask(detail: NodeDetail | null | undefined): EagerTaskInfo | null {
  return detail?.eager && !("applies" in detail.eager) ? detail.eager : null;
}

const BUILD_STATE: Record<EagerState, string> = {
  building: "Building",
  splitting: "Splitting",
  split: "Split into one pull request per task",
  failed: "The build failed",
  split_failed: "The split failed",
};

/** `#20` for a pull request URL. */
function prLabel(url: string): string {
  const match = /\/pull\/(\d+)/.exec(url);
  return match ? `#${match[1]}` : url;
}

function taskState(task: EagerTask, autoRedo: boolean): string {
  if (task.state === "merged") return "Merged";
  if (task.state === "stale") return autoRedo ? "Stale: will be rebuilt" : "Stale: press Redo";
  return "Open";
}

export function EagerSection({
  eager,
  onRedo,
  onGoTo,
}: {
  eager: EagerFeature;
  onRedo: () => Promise<void>;
  onGoTo: (id: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { build, pending } = eager;

  const redo = async () => {
    setBusy(true);
    setError(null);
    try {
      await onRedo();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="eager">
      <div className="section-head">
        <h3>Eager build</h3>
        {build && (
          <button onClick={() => void redo()} disabled={busy || eager.redo.action === null} title={eager.redo.why}>
            {busy && <Spinner />}
            {eager.redo.action === "split" ? "Split again" : "Redo"}
          </button>
        )}
      </div>
      {!build ? (
        <p className="hint">
          {eager.applies
            ? "Eager mode is on: Implement builds every To do task of this feature in one run, on one branch, then splits it into one pull request per task, each targeting the one before."
            : "Eager mode is off for this feature."}
        </p>
      ) : (
        <dl className="delivery">
          <dt>State</dt>
          <dd className={cx(build.state.endsWith("failed") && "no-pr")}>
            {BUILD_STATE[build.state]}
            {build.round > 1 && ` (round ${build.round})`}
          </dd>
          <dt>Branch</dt>
          <dd>
            <code>{build.branch}</code>
            {build.sha && <code> {build.sha.slice(0, 8)}</code>}
          </dd>
          <dt>From</dt>
          <dd>
            <code>{build.base}</code> at <code>{build.base_sha.slice(0, 8)}</code>
          </dd>
        </dl>
      )}
      {build?.error && <div className="callout warn">{build.error}</div>}
      {pending?.node && (
        <div className="callout warn">
          <span>
            <strong>{pending.node.title}</strong>{" "}
            {pending.kind === "merged" ? "merged with changes" : "has new commits"} since the split, so the tasks after it are stale.{" "}
            {pending.blocked ? "" : eager.auto_redo ? "APM rebuilds them on its own." : "Press Redo to rebuild them."}
          </span>
        </div>
      )}
      {pending?.blocked && <div className="callout warn">APM won’t rebuild on its own: {pending.blocked}</div>}
      {eager.tasks.length > 0 && (
        <ol className="eager-chain">
          {eager.tasks.map((task) => (
            <li key={task.id}>
              <span className={cx("dot", `state-${task.status === "todo" ? "todo" : task.status}`)} />
              <button className="link" onClick={() => onGoTo(task.id)} title={task.branch}>
                {task.title}
              </button>
              {task.pr ? (
                <a href={task.pr} target="_blank" rel="noreferrer">
                  {prLabel(task.pr)}
                </a>
              ) : (
                <span className="no-pr" title="No pull request is recorded: the task's Notes say why">
                  no PR
                </span>
              )}
              <span className={cx("eager-state", task.state)}>{taskState(task, eager.auto_redo)}</span>
            </li>
          ))}
        </ol>
      )}
      {build && build.warnings.length > 0 && (
        <ul className="eager-warnings">
          {build.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
      {eager.tasks.length > 0 && (
        <p className="hint">Merge them in order: once one merges, APM points the next one’s pull request at the feature branch.</p>
      )}
      {error && <p className="form-error">{error}</p>}
    </section>
  );
}
