// What you can do to a feature's tasks all at once: review them all, or build every one that is
// ready. Runs beyond runner.max_concurrent wait their turn on the server, so neither ever fails for
// lack of room; the result says what started, what waits and what was skipped.

import { useEffect, useMemo, useState } from "react";
import type { Batch, NodeView } from "./types";
import { Spinner } from "./ui";
import { errorText } from "./util";

type Action = "review" | "implement";

/** The To do tasks inside a feature, at any depth: its leaves that aren't review steps. */
function tasksIn(feature: string, nodes: NodeView[]): NodeView[] {
  const children = new Map<string, NodeView[]>();
  for (const node of nodes) {
    if (node.parent) children.set(node.parent, [...(children.get(node.parent) ?? []), node]);
  }
  const tasks: NodeView[] = [];
  const walk = (id: string) => {
    for (const child of children.get(id) ?? []) {
      if (child.children > 0) walk(child.id);
      else if (child.kind !== "review" && child.status === "todo") tasks.push(child);
    }
  };
  walk(feature);
  return tasks;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function FeatureControls({
  feature,
  nodes,
  eager = false,
  onReviewTasks,
  onImplementAll,
}: {
  feature: NodeView;
  nodes: NodeView[];
  /** Eager mode applies and nothing is split yet: Implement all builds every To do task at once. */
  eager?: boolean;
  onReviewTasks: (id: string) => Promise<Batch>;
  onImplementAll: (id: string) => Promise<Batch>;
}) {
  const tasks = useMemo(() => tasksIn(feature.id, nodes), [feature.id, nodes]);
  // An eager build covers blocked tasks too: it builds them all in dependency order.
  const ready = eager ? tasks : tasks.filter((t) => t.ready);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState<Action | null>(null);
  const [result, setResult] = useState<{ action: Action; batch: Batch } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setConfirming(false);
    setResult(null);
    setError(null);
  }, [feature.id]);

  const start = async (action: Action) => {
    setConfirming(false);
    setBusy(action);
    setError(null);
    try {
      const batch = await (action === "review" ? onReviewTasks : onImplementAll)(feature.id);
      setResult({ action, batch });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="feature-controls">
      <div className="action-row">
        <span className="row-label">Tasks</span>
        <button
          onClick={() => void start("review")}
          disabled={busy !== null || tasks.length === 0}
          title={
            tasks.length
              ? `Claude reviews the size of every To do task inside, nested features included (${tasks.length}). Up to runner.max_concurrent at once; the rest wait their turn.`
              : "No To do tasks inside"
          }
        >
          {busy === "review" && <Spinner />}
          Review all tasks
        </button>
        {confirming ? (
          <>
            <span className="row-hint">
              {eager
                ? `Build all ${plural(ready.length, "To do task")} in one run, then split ${ready.length === 1 ? "it" : "them"} into one pull request each?`
                : `Build up to ${plural(ready.length, "ready task")}?`}
            </span>
            <button className="primary" onClick={() => void start("implement")}>
              Start
            </button>
            <button onClick={() => setConfirming(false)}>Cancel</button>
          </>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            disabled={busy !== null || ready.length === 0}
            title={
              eager && ready.length
                ? `Eager mode: Claude builds every To do task (${ready.length}) in one run on one branch, once each has passed its task review; APM then splits it into one pull request per task, each targeting the one before.`
                : ready.length
                  ? `Claude builds every task inside that is ready (${ready.length}, stacked ones included) and has passed its task review, each in its own worktree. Up to runner.max_concurrent at once; the rest wait their turn.`
                  : "No task inside is ready"
            }
          >
            {busy === "implement" && <Spinner />}
            Implement all
          </button>
        )}
      </div>
      {result && <BatchResult action={result.action} batch={result.batch} />}
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}

function BatchResult({ action, batch }: { action: Action; batch: Batch }) {
  const what = action === "review" ? "Review all tasks" : "Implement all";
  const parts = [
    `started ${batch.started.length}`,
    batch.waiting.length > 0 && `waiting for room ${batch.waiting.length}`,
    batch.skipped.length > 0 && `skipped ${batch.skipped.length}`,
  ].filter(Boolean);
  const nothing = !batch.started.length && !batch.waiting.length && !batch.skipped.length;
  return (
    <div className="batch-result" role="status">
      <span>
        {what}: {nothing ? "no To do tasks inside." : `${parts.join(", ")}.`}
      </span>
      {batch.skipped.length > 0 && (
        <ul>
          {batch.skipped.map((skip) => (
            <li key={skip.id}>
              <strong>{skip.title}</strong>: {skip.reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
