// The feature report: what a feature is and why, the decisions people made and their reasons, and
// how it was built. APM writes it once the feature's review passes (unless features.report is off)
// or when you press Write report, and commits it to the feature branch as docs/features/…md.

import { useCallback, useEffect, useState } from "react";
import { getReport } from "./api";
import { useLiveEvents } from "./live";
import { Markdown } from "./Markdown";
import type { FeatureSettings, NodeView, ReportDetail, ReportStatus } from "./types";
import { Spinner } from "./ui";
import { errorText, timeAgo } from "./util";

const DETAIL: Record<ReportDetail, string> = { brief: "Brief", standard: "Standard", detailed: "Detailed" };

/** The Write report button, in the feature's actions next to Review all tasks and Implement all. */
export function WriteReportRow({ feature, step, onWrite }: { feature: NodeView; step?: NodeView; onWrite: () => void }) {
  // A feature without a review step (an older plan) may still have a branch: the server says.
  const built = !step || step.delivery !== null;
  return (
    <div className="action-row">
      <span className="row-label">Report</span>
      <button
        onClick={onWrite}
        disabled={!built}
        title={
          built
            ? `Claude writes this feature's report from the plan's record and the code, and APM commits it to ${
                feature.feature_branch ?? "the feature branch"
              }. A new commit each time.`
            : "Nothing is built yet: the feature branch has no work on it"
        }
      >
        Write report
      </button>
    </div>
  );
}

/** Where the feature's report stands, with its link on GitHub and the report itself. `settings`
 * are the feature's own, from its details: they change without a report run, so the line saying
 * whether and how APM will write the report follows them rather than the last fetch.
 */
export function FeatureReportSection({
  featureId,
  settings,
}: {
  featureId: string;
  settings?: Pick<FeatureSettings, "report" | "report_detail">;
}) {
  const [status, setStatus] = useState<ReportStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await getReport(featureId));
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [featureId]);

  useEffect(() => {
    setStatus(null);
    setOpen(false);
    void load();
  }, [load]);

  useLiveEvents((event) => {
    const reporter = (event.type === "run.started" || event.type === "run.finished") && event.run.agent === "apm-feature-reporter";
    if ((reporter && event.run.node_id === featureId) || (event.type === "report.written" && event.node_id === featureId)) {
      void load();
    }
  });

  const report = status?.report ?? null;
  const automatic = settings?.report ?? status?.automatic ?? true;
  const detail = settings?.report_detail ?? status?.detail ?? "standard";
  return (
    <section className="report">
      <div className="section-head">
        <h3>Report</h3>
        {report && (
          <button onClick={() => setOpen(!open)} aria-expanded={open}>
            {open ? "Hide" : "Show"}
          </button>
        )}
      </div>
      {error && <p className="form-error">{error}</p>}
      {!status && !error && <p className="hint">Loading…</p>}
      {status?.state === "writing" && (
        <p className="report-state">
          <Spinner /> Claude is writing it: follow it in the Chat tab.
        </p>
      )}
      {status && !report && status.state !== "writing" && (
        <p className="hint">
          Not written yet.{" "}
          {automatic
            ? `APM writes it once the feature's review passes, as a ${DETAIL[detail].toLowerCase()} report.`
            : "Reports are off for this feature (Settings): APM won't write one on its own."}{" "}
          Press Write report in Actions to write it now.
        </p>
      )}
      {report && <ReportState status={status!} />}
      {report && open && <Markdown text={report.markdown} className="prose report-body" />}
    </section>
  );
}

function ReportState({ status }: { status: ReportStatus }) {
  const report = status.report!;
  const file = status.url ? (
    <a href={status.url} target="_blank" rel="noreferrer">
      {report.path}
    </a>
  ) : (
    <code>{report.path}</code>
  );
  if (report.sha === null) {
    return (
      <div className="callout warn">
        <span>
          Written {timeAgo(report.created)}, but not committed to <code>{report.branch}</code>: {report.error}
        </span>
      </div>
    );
  }
  return (
    <div className="report-state">
      <div>
        {file} · committed to <code>{report.branch}</code> at <code>{report.sha.slice(0, 7)}</code>
      </div>
      <div className="hint">
        {DETAIL[report.detail]} · {timeAgo(report.created)}
        {status.pr && (
          <>
            {" "}
            ·{" "}
            <a href={status.pr} target="_blank" rel="noreferrer">
              the feature's pull request
            </a>
          </>
        )}
        {report.summary && ` · ${report.summary}`}
      </div>
    </div>
  );
}
