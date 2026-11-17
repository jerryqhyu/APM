// Reviewing what the decomposer proposed: edit or drop children, then accept or reject.

import { useState } from "react";
import type { Proposal, ProposedChild } from "./types";
import { Modal } from "./ui";
import { cx } from "./util";

interface Draft extends ProposedChild {
  keep: boolean;
}

export function ProposalReview({
  proposal,
  parentTitle,
  onAccept,
  onReject,
  onClose,
}: {
  proposal: Proposal;
  parentTitle: string;
  onAccept: (children: ProposedChild[]) => Promise<void>;
  onReject: () => Promise<void>;
  onClose: () => void;
}) {
  const [drafts, setDrafts] = useState<Draft[]>(() => proposal.data.children.map((c) => ({ ...c, keep: true })));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const review = proposal.review;
  const kept = drafts.filter((d) => d.keep);
  const keptKeys = new Set(kept.map((d) => d.key));

  const update = (key: string, change: Partial<Draft>) =>
    setDrafts((before) => before.map((d) => (d.key === key ? { ...d, ...change } : d)));

  const act = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  const accept = () =>
    act(() =>
      onAccept(
        // A dependency on a child you dropped goes with it.
        kept.map(({ keep: _, ...child }) => ({
          ...child,
          acceptance: child.acceptance.map((a) => a.trim()).filter(Boolean),
          depends_on_keys: child.depends_on_keys.filter((k) => keptKeys.has(k)),
        })),
      ),
    );

  return (
    <Modal
      wide
      title={`Proposed breakdown of “${parentTitle}”`}
      onClose={onClose}
      footer={
        <>
          {error && <span className="form-error">{error}</span>}
          <button onClick={() => act(onReject)} disabled={busy}>
            Reject
          </button>
          <button className="primary" onClick={accept} disabled={busy || kept.length === 0}>
            Accept {kept.length} {kept.length === 1 ? "child" : "children"}
          </button>
        </>
      }
    >
      {proposal.data.rationale && <p className="rationale">{proposal.data.rationale}</p>}

      {review && (
        <div className={cx("review", review.verdict === "revise" && "revise")}>
          <div className="review-head">
            Reviewer: {review.verdict === "accept" ? "looks sound" : "asked for changes"}
            {review.revised && " (the proposal was revised once in response)"}
          </div>
          {review.summary && <p>{review.summary}</p>}
          {review.issues.length > 0 && (
            <ul>
              {review.issues.map((issue, i) => (
                <li key={i}>
                  <span className={cx("severity", issue.severity)}>{issue.severity}</span>
                  {issue.child_key && <code>{titleOf(drafts, issue.child_key)}</code>} {issue.problem}
                  {issue.suggestion && <span className="suggestion"> Suggestion: {issue.suggestion}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {proposal.data.warnings.length > 0 && (
        <div className="review warn">
          <div className="review-head">Claude flagged</div>
          <ul>
            {proposal.data.warnings.map((warning, i) => (
              <li key={i}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="proposed-list">
        {drafts.map((draft) => (
          <div key={draft.key} className={cx("proposed", !draft.keep && "dropped")}>
            <div className="proposed-head">
              <label className="keep" title={draft.keep ? "Untick to leave this one out" : "Tick to include it"}>
                <input type="checkbox" checked={draft.keep} onChange={(e) => update(draft.key, { keep: e.target.checked })} />
              </label>
              <input
                className="title-input"
                value={draft.title}
                disabled={!draft.keep}
                aria-label="Title"
                onChange={(e) => update(draft.key, { title: e.target.value })}
              />
              <select
                value={draft.kind}
                disabled={!draft.keep}
                aria-label="Kind"
                onChange={(e) => update(draft.key, { kind: e.target.value as ProposedChild["kind"] })}
              >
                <option value="work">Work</option>
                <option value="decision">Decision</option>
              </select>
            </div>
            {draft.keep && (
              <>
                <label className="field">
                  <span>Intent</span>
                  <textarea rows={2} value={draft.intent} onChange={(e) => update(draft.key, { intent: e.target.value })} />
                </label>
                <label className="field">
                  <span>Acceptance criteria (one per line)</span>
                  <textarea
                    rows={Math.max(2, draft.acceptance.length)}
                    value={draft.acceptance.join("\n")}
                    onChange={(e) => update(draft.key, { acceptance: e.target.value.split("\n") })}
                  />
                </label>
                {(draft.depends_on_keys.length > 0 || draft.depends_on_ids.length > 0) && (
                  <div className="needs">
                    Needs first:{" "}
                    {draft.depends_on_keys.map((key) => (
                      <span key={key} className={cx("chip", !keptKeys.has(key) && "struck")}>
                        {titleOf(drafts, key)}
                        <button
                          aria-label="Remove this dependency"
                          onClick={() => update(draft.key, { depends_on_keys: draft.depends_on_keys.filter((k) => k !== key) })}
                        >
                          ✕
                        </button>
                      </span>
                    ))}
                    {draft.depends_on_ids.map((id) => (
                      <span key={id} className="chip existing">
                        {id}
                        <button
                          aria-label="Remove this dependency"
                          onClick={() => update(draft.key, { depends_on_ids: draft.depends_on_ids.filter((k) => k !== id) })}
                        >
                          ✕
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        ))}
      </div>
    </Modal>
  );
}

function titleOf(drafts: Draft[], key: string): string {
  return drafts.find((d) => d.key === key)?.title ?? key;
}
