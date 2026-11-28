// Settings: the project's defaults (a dialog, from the whole-plan actions) and a feature's own
// settings (a section of its details). Both are saved to .apm/apm.yaml, which keeps its comments,
// and each save is one entry in the plan's history.

import { type ReactNode, useEffect, useState } from "react";
import { getSettings, saveSettings } from "./api";
import type {
  Automation,
  FeatureSettings,
  NodeDetail,
  NodeRef,
  NodeView,
  ProjectSettings,
  ReportDetail,
  SettingName,
  SettingsChange,
} from "./types";
import { Modal, Spinner } from "./ui";
import { cx, errorText } from "./util";

/**
 * Settings whose feature isn't built yet: they are saved, and do nothing until it is. Take a name
 * out when its feature lands.
 */
const NOT_BUILT = new Set<string>([]);
const NOT_BUILT_HINT = "Not built yet: saved for when it is.";

const MIN_TASK_LINES = 5;
const DETAILS: ReportDetail[] = ["brief", "standard", "detailed"];
const DETAIL_LABEL: Record<ReportDetail, string> = { brief: "Brief", standard: "Standard", detailed: "Detailed" };

const SETTING: Record<SettingName, { label: string; hint: string }> = {
  eager: {
    label: "Eager mode",
    hint: "Claude builds the whole feature in one run, on one branch, then APM splits that work into one pull request per task, in order. Quicker when the tasks touch the same code; you still review every task's pull request.",
  },
  task_max_lines: {
    label: "Task size limit",
    hint: "How much real code one task may need: tests, comments and blank lines don't count. Every task review holds a task to it, and Claude sizes tasks to it when it breaks a feature down.",
  },
  report: {
    label: "Feature report",
    hint: "Once a feature's review passes, Claude writes a report into its branch, so it lands with the feature: what it is, the decisions you made and why, and how it was built.",
  },
  report_detail: {
    label: "Report detail",
    hint: "Brief is about a page. Standard adds each decision's reasons, how the parts fit together and a paragraph per task. Detailed adds notes file by file, what the review found, and a timeline.",
  },
};

const AUTOMATION: { key: keyof Automation; label: string; hint: string }[] = [
  {
    key: "task_review",
    label: "Review tasks as they change",
    hint: "Whenever a task inside a feature is created, or its title, kind or description changes, Claude checks that it is small and clear enough to build. A task can't be built until a review of what it says now passes.",
  },
  {
    key: "reconcile",
    label: "Reconcile",
    hint: "Marks work done once its pull request has merged, and points a stacked pull request at its new base once the one under it merges. It also runs at once when APM sees a pull request merge.",
  },
  {
    key: "feature_review",
    label: "Review finished features",
    hint: "Once every task of a feature has merged into its feature branch, Claude checks that branch against the feature's description and acceptance criteria.",
  },
  {
    key: "accept_follow_ups",
    label: "Accept follow-ups",
    hint: "When a feature review finds gaps, it proposes follow-up tasks. On, they join the plan at once; off, they wait under Proposals for you.",
  },
  {
    key: "implement_follow_ups",
    label: "Build follow-ups",
    hint: "Once an accepted follow-up passes its task review, Claude builds it and opens its pull request into the feature branch. Only follow-ups: never other tasks.",
  },
  {
    key: "feature_pr",
    label: "Open the feature's pull request",
    hint: "When a feature's review passes, APM opens the pull request from its feature branch into where it belongs: the default branch, or the feature around it.",
  },
  {
    key: "eager_redo",
    label: "Redo eager builds",
    hint: "For an eager feature: when a task's pull request changes after the split (you pushed a fix, say), Claude rebuilds the tasks after it on top of the change and splits them again. Off, they are marked stale and Redo does it by hand.",
  },
];

const AGENTS_HINT =
  "The most Claude runs working at the same time, of every kind: chats, task reviews, builds. Past it, Review all tasks, Implement all and automation wait their turn, and a run you start by hand is refused until one finishes.";

/** An on/off switch. */
export function Switch({ on, onChange, label, disabled }: { on: boolean; onChange: (on: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={cx("toggle", on && "on")}
      disabled={disabled}
      onClick={() => onChange(!on)}
    >
      <span className="toggle-knob" />
    </button>
  );
}

/** One option: its name and what it does on the left, its control on the right. */
function Option({ label, hint, children, error, name }: { label: string; hint: string; children: ReactNode; error?: string | null; name?: string }) {
  return (
    <div className="option">
      <div className="option-text">
        <span className="option-label">{label}</span>
        <p className="option-hint">{hint}</p>
        {name && <NotBuilt name={name} />}
        {error && <span className="form-error">{error}</span>}
      </div>
      <div className="option-control">{children}</div>
    </div>
  );
}

/** The fields of `after` that differ from `before`. */
function changed<T extends object>(before: T, after: T): Partial<T> {
  return Object.fromEntries(Object.entries(after).filter(([key, value]) => before[key as keyof T] !== value)) as Partial<T>;
}

function NotBuilt({ name }: { name: string }) {
  return NOT_BUILT.has(name) ? <span className="setting-note">{NOT_BUILT_HINT}</span> : null;
}

/** The project's defaults for every feature, and what `apm serve` does on its own. */
export function SettingsDialog({
  nodes,
  onSaved,
  onGoTo,
  onClose,
}: {
  nodes: NodeView[];
  onSaved: (said: string) => void;
  onGoTo: (id: string) => void;
  onClose: () => void;
}) {
  const [saved, setSaved] = useState<ProjectSettings | null>(null);
  const [features, setFeatures] = useState<FeatureSettings | null>(null);
  const [automation, setAutomation] = useState<Automation | null>(null);
  const [limit, setLimit] = useState("");
  const [agents, setAgents] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getSettings()
      .then((settings) => {
        setSaved(settings);
        setFeatures(settings.features);
        setAutomation(settings.automation);
        setLimit(String(settings.features.task_max_lines));
        setAgents(String(settings.runner.max_concurrent));
      })
      .catch((err: unknown) => setError(errorText(err)));
  }, []);

  const lines = Number(limit);
  const limitError = !Number.isInteger(lines) || lines < MIN_TASK_LINES ? `A whole number, ${MIN_TASK_LINES} or more.` : null;
  const most = Number(agents);
  const agentsError = !Number.isInteger(most) || most < 1 ? "A whole number, 1 or more." : null;
  const featureChange = saved && features ? changed(saved.features, { ...features, task_max_lines: lines }) : {};
  const automationChange = saved && automation ? changed(saved.automation, automation) : {};
  const runnerChange = saved && most !== saved.runner.max_concurrent ? { max_concurrent: most } : {};
  const dirty = Object.keys(featureChange).length + Object.keys(automationChange).length + Object.keys(runnerChange).length > 0;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await saveSettings({ features: featureChange, automation: automationChange, runner: runnerChange });
      onSaved("Settings saved to apm.yaml.");
      onClose();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };

  const title = (id: string | null, key: string) => (id && nodes.find((n) => n.id === id)?.title) || key;

  return (
    <Modal
      title="Settings"
      wide
      onClose={onClose}
      footer={
        <>
          {error && <span className="form-error">{error}</span>}
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy || !dirty || !!limitError || !!agentsError} onClick={() => void save()}>
            Save
          </button>
        </>
      }
    >
      {!features || !automation || !saved ? (
        !error && (
          <p className="hint">
            <Spinner /> Loading the settings…
          </p>
        )
      ) : (
        <div className="settings">
          <p className="hint">
            Saved to <code>.apm/apm.yaml</code>, keeping its comments, and recorded in the plan’s history.
          </p>

          <section>
            <h3>Every feature</h3>
            <p className="hint">The project’s defaults. A feature can set its own in its Details, for itself and what is inside it.</p>
            <Option label={SETTING.eager.label} hint={SETTING.eager.hint} name="eager">
              <Switch label={SETTING.eager.label} on={features.eager} onChange={(eager) => setFeatures({ ...features, eager })} />
            </Option>
            <Option label={SETTING.task_max_lines.label} hint={SETTING.task_max_lines.hint} error={limitError}>
              <span className="row">
                <input
                  className="number"
                  type="number"
                  min={MIN_TASK_LINES}
                  step={1}
                  value={limit}
                  aria-label="Task size limit"
                  onChange={(e) => setLimit(e.target.value)}
                />
                lines
              </span>
            </Option>
            <Option label={SETTING.report.label} hint={SETTING.report.hint} name="report">
              <Switch label={SETTING.report.label} on={features.report} onChange={(report) => setFeatures({ ...features, report })} />
            </Option>
            <Option label={SETTING.report_detail.label} hint={SETTING.report_detail.hint}>
              <DetailPicker
                value={features.report_detail}
                disabled={!features.report}
                onChange={(report_detail) => setFeatures({ ...features, report_detail })}
              />
            </Option>
          </section>

          <section>
            <h3>Agents</h3>
            <Option label="Agents at once" hint={AGENTS_HINT} error={agentsError}>
              <input
                className="number"
                type="number"
                min={1}
                step={1}
                value={agents}
                aria-label="Agents at once"
                onChange={(e) => setAgents(e.target.value)}
              />
            </Option>
          </section>

          <section>
            <h3>Automation</h3>
            <p className="hint">
              What <code>apm serve</code> does on its own, within the agents at once above.
            </p>
            {AUTOMATION.map(({ key, label, hint }) => (
              <Option key={key} label={label} hint={hint} name={key}>
                <span className="row">
                  {key === "reconcile" && (
                    <>
                      every
                      <input
                        className="number"
                        type="number"
                        min={1}
                        value={automation.reconcile_interval_s}
                        aria-label="Seconds between reconciles"
                        disabled={!automation.reconcile}
                        onChange={(e) => setAutomation({ ...automation, reconcile_interval_s: Number(e.target.value) })}
                      />
                      s
                    </>
                  )}
                  <Switch label={label} on={automation[key] as boolean} onChange={(on) => setAutomation({ ...automation, [key]: on })} />
                </span>
              </Option>
            ))}
          </section>

          {(saved.overrides.length > 0 || saved.problems.length > 0) && (
            <section>
              <h3>Features with their own settings</h3>
              <ul className="refs">
                {saved.overrides.map((override) => (
                  <li key={override.key}>
                    {override.node ? (
                      <button
                        className="link"
                        onClick={() => {
                          onGoTo(override.node!);
                          onClose();
                        }}
                      >
                        {title(override.node, override.key)}
                      </button>
                    ) : (
                      <code>{override.key}</code>
                    )}
                    <span className="ref-status">{describe(override.values)}</span>
                  </li>
                ))}
              </ul>
              {saved.problems.map((problem) => (
                <p key={problem} className="callout warn">
                  {problem}
                </p>
              ))}
            </section>
          )}
        </div>
      )}
    </Modal>
  );
}

function DetailPicker({
  value,
  onChange,
  disabled,
}: {
  value: ReportDetail;
  onChange: (value: ReportDetail) => void;
  disabled?: boolean;
}) {
  return (
    <div className="segmented" role="group" aria-label="Report detail">
      {DETAILS.map((detail) => (
        <button key={detail} className={cx(value === detail && "on")} aria-pressed={value === detail} disabled={disabled} onClick={() => onChange(detail)}>
          {DETAIL_LABEL[detail]}
        </button>
      ))}
    </div>
  );
}

function show(name: SettingName, value: FeatureSettings[SettingName]): string {
  if (typeof value === "boolean") return value ? "on" : "off";
  if (name === "task_max_lines") return `${value} lines`;
  return DETAIL_LABEL[value as ReportDetail].toLowerCase();
}

/** `{eager: true, task_max_lines: 30}` as "Eager mode on · Task size limit 30 lines". */
function describe(values: Partial<FeatureSettings>): string {
  const said = (Object.keys(values) as SettingName[]).map((name) => `${SETTING[name].label} ${show(name, values[name]!)}`);
  return said.join(" · ") || "Nothing";
}

// --- a feature's own settings ---

/**
 * The settings that apply to a feature, each with where it comes from, and a way to set its own:
 * Inherit, or a value of its own. They apply to everything inside it, unless a feature inside
 * sets its own in turn.
 */
export function FeatureSettingsSection({
  detail,
  onSave,
  onClear,
  onGoTo,
}: {
  detail: NodeDetail;
  onSave: (change: SettingsChange<FeatureSettings>) => Promise<void>;
  onClear: () => Promise<void>;
  onGoTo: (id: string) => void;
}) {
  const { own, inherited } = detail.settings;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState(own.task_max_lines?.toString() ?? "");
  useEffect(() => {
    setLimit(own.task_max_lines?.toString() ?? "");
    setError(null);
  }, [detail.id, own.task_max_lines]);

  const run = async (change: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await change();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const save = (change: SettingsChange<FeatureSettings>) => void run(() => onSave(change));

  const from = (name: SettingName): ReactNode => {
    if (own[name] !== undefined) return "Set here";
    return <Source source={inherited.sources[name]} onGoTo={onGoTo} />;
  };
  const inherits = (name: SettingName) => `Inherit: ${show(name, inherited.values[name])}`;
  const saveLimit = () => {
    const text = limit.trim();
    if (text === (own.task_max_lines?.toString() ?? "")) return;
    const lines = Number(text);
    if (text && (!Number.isInteger(lines) || lines < MIN_TASK_LINES)) return setError(`The limit is a whole number, ${MIN_TASK_LINES} or more.`);
    save({ task_max_lines: text ? lines : null });
  };

  return (
    <section className="feature-settings" aria-busy={busy}>
      <div className="section-head">
        <h3>Settings</h3>
        {Object.keys(own).length > 0 && (
          <button onClick={() => void run(onClear)} disabled={busy} title="Remove this feature’s own settings: it inherits them all again">
            Inherit all
          </button>
        )}
      </div>
      <p className="hint">For this feature and everything inside it.</p>
      <div className="setting-table">
        <FeatureSetting name="eager" from={from("eager")}>
          <Toggle value={own.eager} inherit={inherits("eager")} disabled={busy} onChange={(eager) => save({ eager })} />
        </FeatureSetting>
        <FeatureSetting name="task_max_lines" from={from("task_max_lines")}>
          <span className="row">
            <input
              className="number"
              type="number"
              min={MIN_TASK_LINES}
              step={1}
              value={limit}
              placeholder={String(inherited.values.task_max_lines)}
              aria-label="Task size limit"
              title="Leave empty to inherit"
              disabled={busy}
              onChange={(e) => setLimit(e.target.value)}
              onBlur={saveLimit}
              onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
            />
            lines
            {own.task_max_lines !== undefined && (
              <button className="link" disabled={busy} onClick={() => save({ task_max_lines: null })}>
                Inherit
              </button>
            )}
          </span>
        </FeatureSetting>
        <FeatureSetting name="report" from={from("report")}>
          <Toggle value={own.report} inherit={inherits("report")} disabled={busy} onChange={(report) => save({ report })} />
        </FeatureSetting>
        <FeatureSetting name="report_detail" from={from("report_detail")}>
          <select
            value={own.report_detail ?? ""}
            disabled={busy}
            aria-label="Report detail"
            onChange={(e) => save({ report_detail: (e.target.value || null) as ReportDetail | null })}
          >
            <option value="">{inherits("report_detail")}</option>
            {DETAILS.map((detail) => (
              <option key={detail} value={detail}>
                {DETAIL_LABEL[detail]}
              </option>
            ))}
          </select>
        </FeatureSetting>
      </div>
      {error && <p className="form-error">{error}</p>}
    </section>
  );
}

function FeatureSetting({ name, from, children }: { name: SettingName; from: ReactNode; children: ReactNode }) {
  return (
    <>
      <span className="setting-name" title={SETTING[name].hint}>
        {SETTING[name].label}
      </span>
      <div className="setting-control">
        {children}
        <span className="setting-source">
          {from}
          {NOT_BUILT.has(name) && ` · ${NOT_BUILT_HINT}`}
        </span>
      </div>
    </>
  );
}

function Source({ source, onGoTo }: { source: NodeRef | null; onGoTo: (id: string) => void }) {
  if (!source) return <>From the project</>;
  return (
    <>
      From{" "}
      <button className="link" onClick={() => onGoTo(source.id)}>
        {source.title}
      </button>
    </>
  );
}

/** Inherit, On or Off: `value` undefined means inherit. */
function Toggle({
  value,
  inherit,
  disabled,
  onChange,
}: {
  value: boolean | undefined;
  inherit: string;
  disabled: boolean;
  onChange: (value: boolean | null) => void;
}) {
  const options: { label: string; value: boolean | null }[] = [
    { label: inherit, value: null },
    { label: "On", value: true },
    { label: "Off", value: false },
  ];
  return (
    <div className="segmented" role="group">
      {options.map((option) => {
        const on = (value ?? null) === option.value;
        return (
          <button key={option.label} className={cx(on && "on")} aria-pressed={on} disabled={disabled} onClick={() => !on && onChange(option.value)}>
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
