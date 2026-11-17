// Everything about the selected node: what it is, what it waits on, its runs and its history.

import { useEffect, useMemo, useState } from "react";
import { Markdown } from "./Markdown";
import { Transcript } from "./Transcript";
import type { Kind, NodeDetail, NodeRef, NodeView, Proposal, Run, Status } from "./types";
import { Empty, Spinner, StatePill } from "./ui";
import { AGENT_LABEL, RUN_LABEL, STATUS_LABEL, cx, duration, money, stateOf, timeAgo } from "./util";

export interface PanelActions {
  patch: (change: { title?: string; kind?: Kind; status?: Status; body?: string }) => Promise<void>;
  link: (dependsOn: string) => Promise<void>;
  unlink: (dependsOn: string) => Promise<void>;
  open: () => void;
  goTo: (id: string) => void;
  addChild: () => void;
  runAgent: (agent: string) => void;
  move: () => void;
  remove: () => void;
  review: (proposal: Proposal) => void;
  stopRun: (run: Run) => Promise<void>;
  continueRun: (run: Run, message: string) => Promise<void>;
  close: () => void;
}

type Tab = "details" | "runs" | "history";

export function NodePanel({
  detail,
  nodes,
  actions,
  tab,
  onTab,
}: {
  detail: NodeDetail;
  nodes: NodeView[];
  actions: PanelActions;
  tab: Tab;
  onTab: (tab: Tab) => void;
}) {
  const state = stateOf(detail);
  const isLeaf = detail.children === 0;
  const canImplement = isLeaf && (detail.ready || detail.status === "in_progress");
  const running = detail.runs.some((r) => r.status === "running");
  const implementHint = !isLeaf
    ? "Only a node with no children can be built directly"
    : detail.blocked
      ? "Blocked: what it waits on isn't done yet"
      : detail.status === "done" || detail.status === "dropped"
        ? `This node is ${STATUS_LABEL[detail.status].toLowerCase()}`
        : "Claude builds this in its own worktree and opens a pull request";

  return (
    <aside className="panel" aria-label="Node details">
      <header className="panel-head">
        <div className="panel-crumbs">
          {detail.ancestors.map((a) => (
            <span key={a.id}>
              <button className="link" onClick={() => actions.goTo(a.id)}>
                {a.title}
              </button>
              {" › "}
            </span>
          ))}
          <code className="handle" title="Handle: use it with the apm command">
            {detail.handle}
          </code>
          <button className="icon close" onClick={actions.close} aria-label="Close panel">
            ✕
          </button>
        </div>
        <TitleEditor key={detail.id} title={detail.title} onSave={(title) => actions.patch({ title })} />
        <div className="panel-controls">
          <StatePill node={detail} />
          <label>
            <span className="sr-only">Status</span>
            <select value={detail.status} onChange={(e) => void actions.patch({ status: e.target.value as Status })}>
              {(Object.keys(STATUS_LABEL) as Status[]).map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABEL[s]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">Kind</span>
            <select value={detail.kind} onChange={(e) => void actions.patch({ kind: e.target.value as Kind })}>
              <option value="work">Work</option>
              <option value="decision">Decision</option>
            </select>
          </label>
          {detail.children > 0 && (
            <span className="progress-text">
              {detail.progress.done}/{detail.progress.total} done
            </span>
          )}
        </div>
        <div className="panel-actions">
          <button onClick={actions.open} title="Show this node's children">
            Open
          </button>
          <button onClick={actions.addChild}>+ Child</button>
          <button onClick={() => actions.runAgent("apm-decomposer")} disabled={running} title="Ask Claude to break this node down">
            Decompose
          </button>
          <button onClick={() => actions.runAgent("apm-implementer")} disabled={!canImplement || running} title={implementHint}>
            Implement
          </button>
          <button onClick={() => actions.runAgent("apm-assistant")} disabled={running} title="Ask Claude a question about this node">
            Ask
          </button>
          <span className="spacer" />
          <button onClick={actions.move} title="Move under another node">
            Move
          </button>
          <button className="danger" onClick={actions.remove}>
            Delete
          </button>
        </div>
      </header>

      <div className="tabs" role="tablist">
        {(["details", "runs", "history"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={cx("tab", tab === t && "active")} onClick={() => onTab(t)}>
            {t === "details" ? "Details" : t === "runs" ? `Runs${detail.runs.length ? ` (${detail.runs.length})` : ""}` : "History"}
            {t === "runs" && running && <Spinner />}
          </button>
        ))}
      </div>

      <div className="panel-body">
        {tab === "details" && <Details detail={detail} nodes={nodes} actions={actions} stateKey={state.key} />}
        {tab === "runs" && <Runs key={detail.id} detail={detail} actions={actions} />}
        {tab === "history" && <History detail={detail} />}
      </div>
    </aside>
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
    <input
      className="panel-title"
      value={value}
      aria-label="Title"
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
        if (e.key === "Escape") {
          setValue(title);
          e.currentTarget.blur();
        }
      }}
    />
  );
}

function Details({
  detail,
  nodes,
  actions,
  stateKey,
}: {
  detail: NodeDetail;
  nodes: NodeView[];
  actions: PanelActions;
  stateKey: string;
}) {
  return (
    <>
      {detail.proposals.map((proposal) => (
        <div key={proposal.id} className="callout proposal">
          <span>Claude proposed {proposal.data.children.length} children for this node.</span>
          <button className="primary" onClick={() => actions.review(proposal)}>
            Review
          </button>
        </div>
      ))}
      {stateKey === "blocked" && (
        <section>
          <h3>Waiting on</h3>
          <RefList refs={detail.links.waiting_on} onPick={actions.goTo} />
        </section>
      )}
      {detail.dropped_deps.length > 0 && (
        <div className="callout warn">
          This depends on a dropped node, which will never be done. Remove the dependency or point it somewhere else.
        </div>
      )}
      <Body key={detail.id} body={detail.body} onSave={(body) => actions.patch({ body })} />
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
      {detail.delivery && (
        <section>
          <h3>Delivery</h3>
          <dl className="delivery">
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
    </>
  );
}

function Body({ body, onSave }: { body: string; onSave: (body: string) => Promise<void> }) {
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
      ) : body.trim() ? (
        <Markdown text={body} />
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

function Runs({ detail, actions }: { detail: NodeDetail; actions: PanelActions }) {
  const [picked, setPicked] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const runs = detail.runs;
  // By default follow whatever is running; otherwise show the latest run you'd care about, which
  // is not the automatic review.
  const current =
    runs.find((r) => r.id === picked) ??
    runs.find((r) => r.status === "running") ??
    runs.find((r) => r.agent !== "apm-critic") ??
    runs[0] ??
    null;
  const anyRunning = runs.some((r) => r.status === "running");

  if (!current) {
    return <Empty>No runs yet. Use Decompose, Implement or Ask above to put Claude to work on this node.</Empty>;
  }
  const send = async () => {
    setBusy(true);
    setError(null);
    try {
      await actions.continueRun(current, message);
      setMessage("");
      setPicked(null); // follow the new run
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <ul className="runs">
        {runs.map((run) => (
          <li key={run.id}>
            <button className={cx("run", run.id === current.id && "active")} onClick={() => setPicked(run.id)}>
              <span className={cx("run-status", run.status)}>
                {run.status === "running" ? <Spinner /> : null}
                {RUN_LABEL[run.status]}
              </span>
              <span className="run-agent">{AGENT_LABEL[run.agent] ?? run.agent}</span>
              <span className="run-meta">
                {timeAgo(run.started)} · {duration(run.started, run.ended)}
                {run.cost_usd !== null && ` · ${money(run.cost_usd)}`}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <div className="run-view">
        {current.status === "running" && (
          <div className="run-bar">
            <button className="danger" onClick={() => void actions.stopRun(current)}>
              Stop
            </button>
          </div>
        )}
        <Transcript key={current.id} run={current} />
        {current.status !== "running" && current.agent !== "apm-critic" && (
          <div className="follow-up">
            <textarea
              rows={2}
              placeholder="Send a follow-up message into this conversation…"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && message.trim()) void send();
              }}
            />
            <div className="row">
              {error && <span className="form-error">{error}</span>}
              <button className="primary" onClick={() => void send()} disabled={busy || anyRunning || !message.trim()}>
                Send
              </button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

function History({ detail }: { detail: NodeDetail }) {
  if (!detail.history.length) return <Empty>No history.</Empty>;
  return (
    <ul className="history">
      {detail.history.map((entry) => (
        <li key={entry.sha}>
          <div className="history-subject">{entry.subject}</div>
          <div className="history-meta">
            {entry.actor === "user" ? "You" : entry.actor.replace(/^agent:/, "")} · {timeAgo(entry.time)} ·{" "}
            <code>{entry.sha.slice(0, 7)}</code>
          </div>
        </li>
      ))}
    </ul>
  );
}
