// The details page: everything about the node you picked, or a summary of the plan when you
// haven't picked one.

import { useEffect, useMemo, useState } from "react";
import { FeatureReportSection } from "./FeatureReport";
import { EagerSection, eagerFeature } from "./Eager";
import { Markdown, withoutEmptySections } from "./Markdown";
import { TagEditor } from "./NodeList";
import { FeatureSettingsSection } from "./Settings";
import type {
  AgentRequest,
  Batch,
  FeatureSettings,
  Issue,
  Kind,
  Moment,
  NodeDetail,
  NodeRef,
  NodeView,
  Project,
  PrStatus,
  Proposal,
  Run,
  SettingsChange,
  Status,
} from "./types";
import { api } from "./api";
import { Empty, Spinner, StatePill } from "./ui";
import { STATUS_LABEL, cx, errorText, proposalSummary, stackedName, stateOf, timeAgo } from "./util";

export interface DetailActions {
  patch: (change: { title?: string; kind?: Kind; status?: Status; body?: string; tags?: string[] }) => Promise<void>;
  link: (dependsOn: string) => Promise<void>;
  unlink: (dependsOn: string) => Promise<void>;
  goTo: (id: string) => void;
  review: (proposal: Proposal) => void;
  /** A feature's own settings: null inherits one again. */
  saveSettings: (change: SettingsChange<FeatureSettings>) => Promise<void>;
  clearSettings: () => Promise<void>;
  /** Redo on an eager feature. */
  eagerRedo: () => Promise<void>;
  /** Start an agent on a node (Implement, Review task): asks first, as the Actions menu does. */
  runAgent: (agent: string, id: string) => void;
  /** Show a conversation in the Chat tab. */
  openRun: (runId: string) => void;
  /** Start Claude on every ready task of a feature (`POST /api/nodes/{id}/implement`). */
  implementAll: (id: string) => Promise<Batch>;
}

const REVIEW_LABEL: Record<string, string> = {
  pass: "Review passed.",
  too_big: "Too big to build as one task:",
  unclear: "Too unclear to size. Missing:",
  error: "The review failed:",
  stale: "Changed since its last review: run Review task again.",
};

export function NodeDetails({ detail, nodes, actions }: { detail: NodeDetail; nodes: NodeView[]; actions: DetailActions }) {
  const review = detail.review;
  const eager = eagerFeature(detail);
  return (
    <article className="details">
      <div className="kicker">
        {detail.ancestors.map((a) => (
          <span key={a.id}>
            <button className="link" onClick={() => actions.goTo(a.id)}>
              {a.title}
            </button>
            <span className="sep"> / </span>
          </span>
        ))}
        <code className="handle" title="Handle: use it with the apm command">
          {detail.handle}
        </code>
      </div>
      <TitleEditor key={detail.id} title={detail.title} onSave={(title) => actions.patch({ title })} />
      <div className="dateline">
        <StatePill node={detail} />
        <span>{detail.kind === "decision" ? "Decision" : detail.kind === "review" ? "Review step" : "Work"}</span>
        {detail.children > 0 && (
          <span>
            {detail.progress.done} of {detail.progress.total} done
          </span>
        )}
        {detail.under_review && <span className="badge warn">Under review</span>}
      </div>
      <TagEditor key={`tags-${detail.id}`} tags={detail.tags} onSave={(tags) => actions.patch({ tags })} />
      <NextStep detail={detail} nodes={nodes} actions={actions} />

      {detail.proposals.map((proposal) => (
        <div key={proposal.id} className="callout proposal">
          <span>{proposalSummary(proposal, detail.title)}</span>
          <button className="primary" onClick={() => actions.review(proposal)}>
            Review
          </button>
        </div>
      ))}
      {detail.dropped_deps.length > 0 && (
        <div className="callout warn">
          This depends on a dropped node, which will never be done. Remove the dependency or point it somewhere else.
        </div>
      )}

      <Body key={`body-${detail.id}`} body={detail.body} onSave={(body) => actions.patch({ body })} />
      {review && !["none", "running"].includes(review.state) && (
        <TaskReviewSection detail={detail} onReview={() => actions.runAgent("apm-task-reviewer", detail.id)} />
      )}
      {detail.children > 0 && (
        <FeatureSettingsSection detail={detail} onSave={actions.saveSettings} onClear={actions.clearSettings} onGoTo={actions.goTo} />
      )}
      {detail.children > 0 && <FeatureReportSection featureId={detail.id} settings={detail.settings?.values} />}
      {eager && <EagerSection eager={eager} onRedo={actions.eagerRedo} onGoTo={actions.goTo} />}

      <section>
        <h3>Depends on</h3>
        <RefList refs={detail.links.depends_on} onPick={actions.goTo} onRemove={(id) => void actions.unlink(id)} />
        <NodePicker
          nodes={nodes}
          exclude={new Set([detail.id, ...detail.depends_on])}
          placeholder="Add a dependency: type a title…"
          onPick={(id) => void actions.link(id)}
        />
      </section>
      {detail.links.dependents.length > 0 && (
        <section>
          <h3>Needed by</h3>
          <RefList refs={detail.links.dependents} onPick={actions.goTo} />
        </section>
      )}
      {detail.links.children.length > 0 && (
        <section>
          <h3>Inside it</h3>
          <RefList refs={detail.links.children} onPick={actions.goTo} />
        </section>
      )}
      {detail.delivery && (
        <section>
          <h3>Delivery</h3>
          <dl className="delivery">
            {!detail.delivery.pr && detail.delivery.branch && (
              <>
                <dt>Pull request</dt>
                <dd className="no-pr" title="The branch is delivered, but no pull request is recorded. The Notes say why.">
                  None yet
                </dd>
              </>
            )}
            {detail.delivery.pr && (
              <>
                <dt>Pull request</dt>
                <dd>
                  <a href={detail.delivery.pr} target="_blank" rel="noreferrer">
                    {detail.delivery.pr}
                  </a>
                </dd>
              </>
            )}
            {detail.delivery.branch && (
              <>
                <dt>Branch</dt>
                <dd>
                  <code>{detail.delivery.branch}</code>
                </dd>
              </>
            )}
            {detail.delivery.commits.length > 0 && (
              <>
                <dt>Commits</dt>
                <dd>
                  {detail.delivery.commits.map((c) => (
                    <code key={c}>{c.slice(0, 8)} </code>
                  ))}
                </dd>
              </>
            )}
          </dl>
        </section>
      )}
      <Timeline key={`timeline-${detail.id}`} moments={detail.timeline} onOpenRun={actions.openRun} />
    </article>
  );
}

const REVIEW_SHORT: Record<string, string> = {
  pass: "it passed",
  too_big: "it was too big",
  unclear: "it was too unclear to size",
  error: "it failed",
};

/** The most useful thing to know about a node now, and the one thing to do next, by its state. */
function NextStep({ detail, nodes, actions }: { detail: NodeDetail; nodes: NodeView[]; actions: DetailActions }) {
  const state = stateOf(detail);
  if (detail.kind === "review") return null; // a review step says what it waits on in its own sections
  const pr = detail.delivery?.pr ?? null;

  if (detail.children > 0) return <FeatureStep detail={detail} nodes={nodes} actions={actions} />;

  if (state.key === "blocked") {
    return (
      <div className="next-step state-blocked">
        <div className="next-head">
          <span className="next-label">Blocked</span>
          <span>It can start once {detail.links.waiting_on.length === 1 ? "this is" : "these are"} done:</span>
        </div>
        <RefList refs={detail.links.waiting_on} onPick={actions.goTo} />
        {detail.dropped_deps.length > 0 && (
          <p className="next-warn">One of them is dropped and will never be done: remove that dependency, or point it elsewhere.</p>
        )}
      </div>
    );
  }

  if (state.key === "ready" || state.key === "stacked") {
    const needsReview = detail.review !== null && ["none", "stale", "too_big", "unclear", "error"].includes(detail.review.state);
    return (
      <div className={cx("next-step", `state-${state.key}`)}>
        <div className="next-head">
          <span className="next-label">Ready to build</span>
          {state.key === "stacked" && <span>on top of {stackedName(detail)}, which is delivered but not merged</span>}
        </div>
        <div className="next-actions">
          <button
            className="primary"
            disabled={!!detail.implement_blocker || detail.under_review}
            title={detail.implement_blocker ?? "Claude builds it in its own worktree and opens a pull request"}
            onClick={() => actions.runAgent("apm-implementer", detail.id)}
          >
            Implement
          </button>
          {needsReview && (
            <button onClick={() => actions.runAgent("apm-task-reviewer", detail.id)} disabled={detail.under_review}>
              Review task
            </button>
          )}
        </div>
        {detail.implement_blocker && <p className="hint">Not yet: {detail.implement_blocker}.</p>}
        {detail.under_review && <p className="hint">Its task review is running.</p>}
      </div>
    );
  }

  if (state.key === "in_progress") {
    const building = detail.builder;
    return (
      <div className="next-step state-in_progress">
        <div className="next-head">
          <span className="next-label">In progress</span>
          <span>
            {building?.status === "running"
              ? "Claude is building it now."
              : building
                ? "Claude worked on it; it has no pull request yet."
                : "Set in progress by hand: no conversation is building it."}
          </span>
        </div>
        {building && (
          <div className="next-actions">
            <button className="primary" onClick={() => actions.openRun(building.id)}>
              {building.status === "running" && <Spinner />}
              Open the conversation building it
            </button>
          </div>
        )}
      </div>
    );
  }

  if (state.key === "review") {
    return <PendingReview detail={detail} actions={actions} />;
  }

  if (state.key === "done") {
    return (
      <div className="next-step state-done">
        <div className="next-head">
          <span className="next-label">Done</span>
          {pr && <PrLink url={pr} />}
        </div>
      </div>
    );
  }
  return null;
}

/** A feature's tasks, by what each needs from you, and Implement all ready tasks. */
function FeatureStep({ detail, nodes, actions }: { detail: NodeDetail; nodes: NodeView[]; actions: DetailActions }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Batch | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setConfirming(false);
    setResult(null);
    setError(null);
  }, [detail.id]);

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const inside = (node: NodeView) => {
    for (let up = node.parent; up; up = byId.get(up)?.parent ?? null) if (up === detail.id) return true;
    return false;
  };
  const tasks = nodes.filter((n) => n.children === 0 && n.kind !== "review" && inside(n));
  const by = (key: string) => tasks.filter((n) => stateOf(n).key === key);
  const ready = [...by("ready"), ...by("stacked")];
  const groups: [string, NodeView[]][] = [
    ["Waiting for your PR review", by("review")],
    ["Ready to build", ready],
    ["Being built", by("in_progress")],
    ["Blocked", by("blocked")],
  ];
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await actions.implementAll(detail.id));
      setConfirming(false);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  // The group says where each task stands: no status on the rows.
  const refs = (list: NodeView[]): NodeRef[] => list.map((n) => ({ id: n.id, handle: n.handle, title: n.title }));
  return (
    <div className="next-step state-in_progress feature-step">
      <div className="next-head">
        <span className="next-label">Feature</span>
        <span>
          {detail.progress.done} of {detail.progress.total} done
        </span>
      </div>
      <div className="progress-bar" aria-hidden>
        <span style={{ width: `${detail.progress.total ? (100 * detail.progress.done) / detail.progress.total : 0}%` }} />
      </div>
      <div className="next-actions">
        {confirming ? (
          <>
            <span>
              Start Claude on {ready.length} ready {ready.length === 1 ? "task" : "tasks"}, each in its own conversation?
            </span>
            <button className="primary" onClick={() => void start()} disabled={busy}>
              {busy && <Spinner />} Start
            </button>
            <button onClick={() => setConfirming(false)} disabled={busy}>
              Cancel
            </button>
          </>
        ) : (
          <button
            className="primary"
            disabled={ready.length === 0}
            title={ready.length ? "Claude builds each ready task in its own worktree and opens its pull request" : "No task is ready"}
            onClick={() => setConfirming(true)}
          >
            Implement all ready tasks ({ready.length})
          </button>
        )}
      </div>
      {error && <p className="form-error">{error}</p>}
      {result && (
        <div className="batch-result">
          {result.started.length > 0 && (
            <p>
              Started:{" "}
              {result.started.map((run, i) => (
                <span key={run.id}>
                  {i > 0 && ", "}
                  <button className="link" onClick={() => actions.openRun(run.id)}>
                    {byId.get(run.node_id)?.title ?? run.node_id}
                  </button>
                </span>
              ))}
            </p>
          )}
          {result.waiting.length > 0 && <p className="hint">Waiting for room: {result.waiting.map((n) => n.title).join(", ")}.</p>}
          {result.skipped.length > 0 && (
            <ul className="hint">
              {result.skipped.map((s) => (
                <li key={s.id}>
                  {s.title}: {s.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {groups.map(
        ([label, list]) =>
          list.length > 0 && (
            <div key={label} className="feature-group">
              <h4>
                {label} ({list.length})
              </h4>
              <RefList refs={refs(list)} onPick={actions.goTo} />
            </div>
          ),
      )}
    </div>
  );
}

/** Delivered, waiting for you: the pull request, where it stands on GitHub, and who built it. */
function PendingReview({ detail, actions }: { detail: NodeDetail; actions: DetailActions }) {
  const url = detail.delivery?.pr ?? null;
  const [status, setStatus] = useState<PrStatus | null>(detail.pr);
  useEffect(() => {
    setStatus(detail.pr);
    if (!url) return;
    let live = true;
    api<PrStatus | null>(`/api/nodes/${detail.id}/pr`)
      .then((fresh) => live && fresh && setStatus(fresh))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [detail.id, url, detail.pr]);
  return (
    <div className="next-step state-review">
      <div className="next-head">
        <span className="next-label">Pending PR review</span>
        <span>{url ? "Review and merge its pull request; APM marks it done once it merges." : "Its branch is pushed, but no pull request is recorded: the Notes say why."}</span>
      </div>
      {url && (
        <div className="next-actions">
          <a className="button primary" href={url} target="_blank" rel="noreferrer">
            Review pull request #{url.split("/").pop()}
          </a>
          {status && <PrChips status={status} />}
        </div>
      )}
      {!url && detail.delivery?.branch && (
        <p className="hint">
          Branch <code>{detail.delivery.branch}</code>
        </p>
      )}
      {detail.builder && (
        <button className="link" onClick={() => actions.openRun(detail.builder!.id)}>
          Open the conversation that built it
        </button>
      )}
    </div>
  );
}

const PR_STATE: Record<PrStatus["state"], string> = { open: "Open", draft: "Draft", merged: "Merged", closed: "Closed" };
const PR_REVIEW: Record<PrStatus["review"], string> = {
  approved: "Approved",
  changes_requested: "Changes requested",
  waiting: "Waiting for review",
};
const PR_CHECKS: Record<PrStatus["checks"], string> = {
  passing: "Checks pass",
  failing: "Checks fail",
  running: "Checks running",
  none: "",
};

/** Where a pull request stands on GitHub, as small chips. */
export function PrChips({ status }: { status: PrStatus }) {
  const open = status.state === "open" || status.state === "draft";
  return (
    <span className="pr-chips" title={`As GitHub said ${timeAgo(status.checked)}`}>
      <span className={cx("chip", `pr-${status.state}`)}>{PR_STATE[status.state]}</span>
      {open && <span className={cx("chip", `pr-${status.review}`)}>{PR_REVIEW[status.review]}</span>}
      {open && status.checks !== "none" && <span className={cx("chip", `checks-${status.checks}`)}>{PR_CHECKS[status.checks]}</span>}
    </span>
  );
}

function PrLink({ url }: { url: string }) {
  return (
    <a href={url} target="_blank" rel="noreferrer">
      Pull request #{url.split("/").pop()}
    </a>
  );
}

/** The task review, after the description: its verdict, or, once the task changed, what changed. */
function TaskReviewSection({ detail, onReview }: { detail: NodeDetail; onReview: () => void }) {
  const review = detail.review!;
  const when = review.reviewed_at ? ` ${timeAgo(review.reviewed_at)}` : "";
  if (review.state === "stale") {
    const changes = review.changes;
    return (
      <section className="task-review">
        <h3>Task review</h3>
        <div className="callout warn">
          <span>
            <strong>Changed since its last review</strong>
            {when}
            {review.verdict ? `, when ${REVIEW_SHORT[review.verdict] ?? review.verdict}` : ""}.{" "}
            {changes && changes.length > 0 ? `What changed: ${changes.join("; ")}.` : ""}
          </span>
          <button onClick={onReview} disabled={detail.under_review}>
            Review task
          </button>
        </div>
      </section>
    );
  }
  return (
    <section className="task-review">
      <h3>Task review</h3>
      <div className={cx("callout", review.state !== "pass" && "warn")}>
        <span>
          <strong>{REVIEW_LABEL[review.state]}</strong> {review.error ?? review.summary}
          {review.missing && review.missing.length > 0 && (
            <ul>
              {review.missing.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          )}
          {when && <span className="hint"> Reviewed{when}.</span>}
        </span>
      </div>
    </section>
  );
}

/** What happened to the node, newest first: the latest few, and the rest on request. */
function Timeline({ moments, onOpenRun }: { moments: Moment[]; onOpenRun: (runId: string) => void }) {
  const [all, setAll] = useState(false);
  const newest = [...moments].reverse();
  const shown = all ? newest : newest.slice(0, 8);
  return (
    <section>
      <h3>Timeline</h3>
      {newest.length === 0 && <Empty>Nothing yet.</Empty>}
      <ol className="timeline">
        {shown.map((moment, i) => (
          <li key={`${moment.time}-${i}`}>
            <span className="timeline-dot" aria-hidden />
            <span className="timeline-text">
              {moment.text}
              {moment.url && (
                <>
                  {" "}
                  <a href={moment.url} target="_blank" rel="noreferrer">
                    #{moment.url.split("/").pop()}
                  </a>
                </>
              )}
            </span>
            <span className="timeline-meta">
              {moment.who} · {timeAgo(moment.time)}
              {moment.run && (
                <>
                  {" · "}
                  <button className="link" onClick={() => onOpenRun(moment.run!)}>
                    conversation
                  </button>
                </>
              )}
            </span>
          </li>
        ))}
      </ol>
      {newest.length > shown.length && (
        <button className="link" onClick={() => setAll(true)}>
          Show all {newest.length}
        </button>
      )}
    </section>
  );
}

/** With nothing picked at the top level: what needs you, then what is moving, then what waits. */
export function PlanSummary({
  project,
  nodes,
  issues,
  proposals,
  requests,
  runs,
  onGoTo,
  onReview,
  onOpenRun,
  onImplement,
}: {
  project: Project;
  nodes: NodeView[];
  issues: Issue[];
  proposals: Proposal[];
  /** Questions and permission prompts Claude is waiting on. */
  requests: AgentRequest[];
  /** The runs going now, to say what a request is about. */
  runs: Run[];
  onGoTo: (id: string) => void;
  onReview: (proposal: Proposal) => void;
  onOpenRun: (runId: string) => void;
  onImplement: (id: string) => void;
}) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const leaves = nodes.filter((n) => n.children === 0 && n.status !== "dropped" && n.kind !== "review");
  const done = leaves.filter((n) => n.status === "done").length;
  const by = (...keys: string[]) => leaves.filter((n) => keys.includes(stateOf(n).key));
  const toReview = by("review");
  const ready = by("ready", "stacked");
  const building = by("in_progress");
  const blocked = by("blocked");
  const needsYou = requests.length + proposals.length + toReview.length;
  const title = (id: string) => byId.get(id)?.title ?? "a deleted node";
  return (
    <article className="details inbox">
      <h2 className="headline">{project.name}</h2>
      <div className="dateline">
        <span>
          {done} of {leaves.length} tasks done
        </span>
        <span>{needsYou ? `${needsYou} need${needsYou === 1 ? "s" : ""} you` : "Nothing needs you"}</span>
      </div>
      {nodes.length === 0 && (
        <p className="hint">
          Nothing here yet. Add a node for the big idea, or tell Claude what you want to build in the Chat tab.
        </p>
      )}

      {needsYou > 0 && (
        <section>
          <h3>Needs you</h3>
          <ul className="inbox-list">
            {requests.map((request) => {
              const run = runs.find((r) => r.id === request.run_id);
              return (
                <li key={request.id}>
                  <span className="dot state-review" />
                  <span className="inbox-text">
                    Claude {request.kind === "question" ? "has a question" : request.kind === "plan" ? "has a plan to approve" : "asks permission"}{" "}
                    {run?.node_id ? `about “${title(run.node_id)}”` : "in a chat about the plan"}
                  </span>
                  <button className="primary" onClick={() => onOpenRun(request.run_id)}>
                    Answer
                  </button>
                </li>
              );
            })}
            {proposals.map((proposal) => (
              <li key={proposal.id}>
                <span className="dot state-proposed" />
                <span className="inbox-text">{proposalSummary(proposal, title(proposal.node_id))}</span>
                <button className="primary" onClick={() => onReview(proposal)}>
                  Review
                </button>
              </li>
            ))}
            {toReview.map((node) => (
              <li key={node.id}>
                <span className="dot state-review" />
                <span className="inbox-text">
                  <button className="link" onClick={() => onGoTo(node.id)}>
                    {node.title}
                  </button>{" "}
                  {node.pr && <PrChips status={node.pr} />}
                </span>
                {node.delivery?.pr ? (
                  <a className="button primary" href={node.delivery.pr} target="_blank" rel="noreferrer">
                    Review PR #{node.delivery.pr.split("/").pop()}
                  </a>
                ) : (
                  <span className="hint">no pull request</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {ready.length > 0 && (
        <section>
          <h3>Ready to build ({ready.length})</h3>
          <ul className="inbox-list">
            {ready.map((node) => (
              <li key={node.id}>
                <span className={cx("dot", `state-${stateOf(node).key}`)} />
                <span className="inbox-text">
                  <button className="link" onClick={() => onGoTo(node.id)}>
                    {node.title}
                  </button>
                  {node.stack_top && <span className="hint"> · on top of {stackedName(node)}</span>}
                </span>
                <button onClick={() => onImplement(node.id)}>Implement</button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {building.length > 0 && (
        <section>
          <h3>Being built ({building.length})</h3>
          <RefList refs={building.map((n) => ({ id: n.id, handle: n.handle, title: n.title, status: n.status }))} onPick={onGoTo} />
        </section>
      )}

      {blocked.length > 0 && (
        <section>
          <h3>Blocked ({blocked.length})</h3>
          <ul className="inbox-list">
            {blocked.map((node) => (
              <li key={node.id}>
                <span className="dot state-blocked" />
                <span className="inbox-text">
                  <button className="link" onClick={() => onGoTo(node.id)}>
                    {node.title}
                  </button>
                  <span className="hint"> · waits on {node.waiting_on.map(title).join(", ")}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {issues.length > 0 && (
        <section>
          <h3>Problems</h3>
          <ul className="issues">
            {issues.map((issue, i) => (
              <li key={i}>
                <button className="link" onClick={() => issue.nodes[0] && onGoTo(issue.nodes[0])}>
                  {issue.message}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </article>
  );
}

function TitleEditor({ title, onSave }: { title: string; onSave: (title: string) => Promise<void> }) {
  const [value, setValue] = useState(title);
  useEffect(() => setValue(title), [title]);
  const commit = () => {
    const next = value.trim();
    if (next && next !== title) void onSave(next).catch(() => setValue(title));
    else setValue(title);
  };
  return (
    <textarea
      className="headline headline-input"
      id="node-title"
      rows={1}
      value={value}
      aria-label="Title"
      onChange={(e) => setValue(e.target.value.replace(/\n/g, " "))}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          e.currentTarget.blur();
        }
        if (e.key === "Escape") {
          setValue(title);
          e.currentTarget.blur();
        }
      }}
    />
  );
}

function Body({ body, onSave }: { body: string; onSave: (body: string) => Promise<void> }) {
  const shown = useMemo(() => withoutEmptySections(body), [body]);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(body);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!editing) setDraft(body);
  }, [body, editing]);
  const save = async () => {
    setSaving(true);
    try {
      await onSave(draft.endsWith("\n") ? draft : `${draft}\n`);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };
  return (
    <section className="body">
      <div className="section-head">
        <h3>Description</h3>
        {editing ? (
          <span className="row">
            <button onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
            <button className="primary" onClick={() => void save()} disabled={saving || draft === body}>
              Save
            </button>
          </span>
        ) : (
          <button onClick={() => setEditing(true)}>Edit</button>
        )}
      </div>
      {editing ? (
        <textarea
          className="body-editor"
          value={draft}
          autoFocus
          spellCheck
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void save();
          }}
        />
      ) : shown ? (
        <Markdown text={shown} className="prose" />
      ) : (
        <Empty>No description yet.</Empty>
      )}
    </section>
  );
}

function RefList({ refs, onPick, onRemove }: { refs: NodeRef[]; onPick: (id: string) => void; onRemove?: (id: string) => void }) {
  if (!refs.length) return <Empty>None.</Empty>;
  return (
    <ul className="refs">
      {refs.map((ref) => (
        <li key={ref.id}>
          <span className={cx("dot", `state-${ref.status === "todo" ? "todo" : ref.status}`)} />
          <button className="link" onClick={() => onPick(ref.id)}>
            {ref.title}
          </button>
          {ref.status && <span className="ref-status">{STATUS_LABEL[ref.status]}</span>}
          {onRemove && (
            <button className="icon" onClick={() => onRemove(ref.id)} aria-label={`Remove dependency on ${ref.title}`}>
              ✕
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

export function NodePicker({
  nodes,
  exclude,
  placeholder,
  onPick,
  autoFocus,
}: {
  nodes: NodeView[];
  exclude: Set<string>;
  placeholder: string;
  onPick: (id: string) => void;
  autoFocus?: boolean;
}) {
  const [text, setText] = useState("");
  const matches = useMemo(() => {
    const terms = text.toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) return [];
    return nodes
      .filter((n) => !exclude.has(n.id) && terms.every((t) => n.title.toLowerCase().includes(t) || n.handle.includes(t)))
      .slice(0, 8);
  }, [text, nodes, exclude]);
  return (
    <div className="picker">
      <input value={text} placeholder={placeholder} autoFocus={autoFocus} onChange={(e) => setText(e.target.value)} />
      {text && (
        <ul className="picker-list">
          {matches.map((node) => (
            <li key={node.id}>
              <button
                onClick={() => {
                  onPick(node.id);
                  setText("");
                }}
              >
                <span className={cx("dot", `state-${stateOf(node).key}`)} />
                {node.title} <code>{node.handle}</code>
              </button>
            </li>
          ))}
          {!matches.length && <li className="empty">No node matches.</li>}
        </ul>
      )}
    </div>
  );
}
