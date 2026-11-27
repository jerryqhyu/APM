// The details page: everything about the node you picked, or a summary of the plan when you
// haven't picked one.

import { useEffect, useMemo, useState } from "react";
import { FeatureReportSection } from "./FeatureReport";
import { EagerSection, eagerFeature } from "./Eager";
import { Markdown, withoutEmptySections } from "./Markdown";
import { TagEditor } from "./NodeList";
import { FeatureSettingsSection } from "./Settings";
import type {
  FeatureSettings,
  Issue,
  Kind,
  NodeDetail,
  NodeRef,
  NodeView,
  Project,
  Proposal,
  SettingsChange,
  Status,
} from "./types";
import { Empty, StatePill } from "./ui";
import { STATUS_LABEL, cx, proposalSummary, stackedName, stateOf, timeAgo } from "./util";

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
}

const REVIEW_LABEL: Record<string, string> = {
  pass: "Review passed.",
  too_big: "Too big to build as one task:",
  unclear: "Too unclear to size. Missing:",
  error: "The review failed:",
  stale: "Changed since its last review: run Review task again.",
};

export function NodeDetails({ detail, nodes, actions }: { detail: NodeDetail; nodes: NodeView[]; actions: DetailActions }) {
  const state = stateOf(detail);
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
      {review && !["none", "running"].includes(review.state) && (
        <div className={cx("callout", review.state !== "pass" && "warn")}>
          <strong>{REVIEW_LABEL[review.state]}</strong> {review.error ?? review.summary}
          {review.missing && review.missing.length > 0 && <ul>{review.missing.map((m) => <li key={m}>{m}</li>)}</ul>}
        </div>
      )}

      {detail.proposals.map((proposal) => (
        <div key={proposal.id} className="callout proposal">
          <span>{proposalSummary(proposal, detail.title)}</span>
          <button className="primary" onClick={() => actions.review(proposal)}>
            Review
          </button>
        </div>
      ))}
      {state.key === "blocked" && (
        <section>
          <h3>Waiting on</h3>
          <RefList refs={detail.links.waiting_on} onPick={actions.goTo} />
        </section>
      )}
      {state.key === "stacked" && (
        <section>
          <h3>Stacked on</h3>
          <p className="hint">
            This can start now, on top of work that is delivered but not merged. Implement builds it on the branch of{" "}
            {stackedName(detail)}, and its pull request targets that branch, so it shows only its own changes. It is plainly
            ready once that work merges.
          </p>
          <RefList
            refs={detail.links.waiting_on.filter((ref) => detail.stacked_on.some((s) => s.id === ref.id))}
            onPick={actions.goTo}
          />
        </section>
      )}
      {detail.dropped_deps.length > 0 && (
        <div className="callout warn">
          This depends on a dropped node, which will never be done. Remove the dependency or point it somewhere else.
        </div>
      )}

      <Body key={`body-${detail.id}`} body={detail.body} onSave={(body) => actions.patch({ body })} />
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
      <section>
        <h3>History</h3>
        <ul className="history">
          {detail.history.map((entry) => (
            <li key={entry.sha}>
              <span className="history-subject">{entry.subject}</span>
              <span className="history-meta">
                {entry.actor === "user" ? "You" : entry.actor.replace(/^agent:apm-/, "Claude · ").replace(/^agent:/, "")},{" "}
                {timeAgo(entry.time)}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </article>
  );
}

export function PlanSummary({
  project,
  nodes,
  issues,
  proposals,
  onGoTo,
  onReview,
}: {
  project: Project;
  nodes: NodeView[];
  issues: Issue[];
  proposals: Proposal[];
  onGoTo: (id: string) => void;
  onReview: (proposal: Proposal) => void;
}) {
  const leaves = nodes.filter((n) => n.children === 0 && n.status !== "dropped");
  const done = leaves.filter((n) => n.status === "done").length;
  const ready = nodes.filter((n) => n.ready && n.children === 0);
  const inProgress = nodes.filter((n) => n.status === "in_progress" && n.children === 0);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const refs = (list: NodeView[]): NodeRef[] => list.map((n) => ({ id: n.id, handle: n.handle, title: n.title, status: n.status }));
  return (
    <article className="details">
      <h2 className="headline">{project.name}</h2>
      <div className="dateline">
        <span>
          {done} of {leaves.length} pieces of work done
        </span>
        <span>{nodes.length} nodes</span>
      </div>
      {nodes.length === 0 && (
        <p className="hint">
          Nothing here yet. Add a node for the big idea, or tell Claude what you want to build in the Chat tab.
        </p>
      )}
      {proposals.map((proposal) => (
        <div key={proposal.id} className="callout proposal">
          <span>{proposalSummary(proposal, byId.get(proposal.node_id)?.title ?? "a deleted node")}</span>
          <button className="primary" onClick={() => onReview(proposal)}>
            Review
          </button>
        </div>
      ))}
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
      {inProgress.length > 0 && (
        <section>
          <h3>In progress</h3>
          <RefList refs={refs(inProgress)} onPick={onGoTo} />
        </section>
      )}
      {nodes.length > 0 && (
        <section>
          <h3>Ready to start</h3>
          <RefList refs={refs(ready.filter((n) => !n.stack_top))} onPick={onGoTo} />
          {ready.some((n) => n.stack_top) && (
            <>
              <h3>Ready, stacked on open work</h3>
              <RefList refs={refs(ready.filter((n) => n.stack_top))} onPick={onGoTo} />
            </>
          )}
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
