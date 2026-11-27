// The shapes the server returns (src/apm/core/views.py and src/apm/server/app.py).

export type Status = "todo" | "in_progress" | "done" | "dropped";
export type Kind = "work" | "decision" | "review";

export interface Delivery {
  repo: string;
  branch: string | null;
  commits: string[];
  pr: string | null;
}

/** A dependency a node stacks on: delivered (a branch or a PR) but not merged yet. */
export interface StackedOn {
  id: string;
  handle: string;
  /** `#20`, or null if it has no pull request yet. */
  pr: string | null;
}

export interface NodeView {
  id: string;
  handle: string;
  parent: string | null;
  title: string;
  kind: Kind;
  status: Status;
  /** Labels, sorted: lowercase letters, digits and `. _ / -`. */
  tags: string[];
  blocked: boolean;
  ready: boolean;
  waiting_on: string[];
  /** Ready because of these: start on top of their branch. Empty when everything it needs is done. */
  stacked_on: StackedOn[];
  /** Which of `stacked_on` to build on: its branch holds them all. Null if it can't stack. */
  stack_top: string | null;
  dropped_deps: string[];
  depends_on: string[];
  dependents: string[];
  children: number;
  progress: { done: number; total: number };
  delivery: Delivery | null;
  /** A feature's review step: the child that waits on all the others. Null for a leaf. */
  review_step: string | null;
  /** The feature's branch, once APM has made it. */
  feature_branch: string | null;
  /** Where its pull request stands on GitHub, as last seen. Null when unknown or it has none. */
  pr: PrStatus | null;
}

/** A pull request on GitHub (src/apm/core/pr_status.py). */
export interface PrStatus {
  url: string;
  state: "open" | "draft" | "merged" | "closed";
  review: "approved" | "changes_requested" | "waiting";
  checks: "passing" | "failing" | "running" | "none";
  checked: number;
}

/** One moment of a node's story (src/apm/core/timeline.py). */
export interface Moment {
  time: number;
  text: string;
  who: string;
  run: string | null;
  url: string | null;
}

export interface NodeRef {
  id: string;
  handle: string;
  title: string;
  status?: Status;
}

export interface Issue {
  code: string;
  message: string;
  nodes: string[];
}

export interface Tree {
  head: string | null;
  nodes: NodeView[];
  errors: Issue[];
  warnings: Issue[];
}

export interface LevelEdge {
  src: string;
  dst: string;
  count: number;
}

export interface LevelPort {
  node: string;
  outside: NodeRef;
  direction: "in" | "out";
  count: number;
}

export interface Level {
  root: NodeView | null;
  breadcrumb: NodeRef[];
  nodes: NodeView[];
  edges: LevelEdge[];
  ports: LevelPort[];
  entangled: string[][];
}

export interface HistoryEntry {
  sha: string;
  time: number;
  subject: string;
  op: string;
  actor: string;
  run: string | null;
}

export type RunStatus = "running" | "succeeded" | "failed" | "cancelled";

export interface Run {
  id: string;
  node_id: string;
  agent: string;
  session_id: string;
  status: RunStatus;
  started: number;
  ended: number | null;
  cost_usd: number | null;
  error: string | null;
  prompt: string;
  parent_run: string | null;
  /** The permission mode it ran in. */
  mode: string;
}

export interface RunEvent {
  seq: number;
  ts: number;
  data: Record<string, unknown>;
}

export interface ProposedChild {
  key: string;
  title: string;
  kind: Kind;
  intent: string;
  /** What a builder must know first. Older proposals have none. */
  context?: string;
  /** How to build it. Older proposals have none. */
  approach?: string;
  acceptance: string[];
  out_of_scope?: string[];
  depends_on_keys: string[];
  depends_on_ids: string[];
}

export interface ReviewIssue {
  severity: string;
  child_key: string | null;
  problem: string;
  suggestion: string;
}

export interface Proposal {
  id: string;
  node_id: string;
  run_id: string | null;
  data: { rationale: string; children: ProposedChild[]; warnings: string[]; split_of?: { title: string } };
  review: { verdict: string; summary: string; issues: ReviewIssue[]; revised?: boolean } | null;
  status: "pending" | "accepted" | "rejected";
  created: number;
  /** A task review's split: accepted, its parts replace the task in its feature. */
  split?: boolean;
}

export interface NodeDetail extends NodeView {
  body: string;
  ancestors: NodeRef[];
  links: { depends_on: NodeRef[]; dependents: NodeRef[]; waiting_on: NodeRef[]; children: NodeRef[] };
  history: HistoryEntry[];
  proposals: Proposal[];
  under_review: boolean;
  /** Null while task review is off. */
  review: {
    state: "none" | "running" | "stale" | "pass" | "too_big" | "unclear" | "error";
    verdict?: "pass" | "too_big" | "unclear" | "error";
    summary?: string;
    missing?: string[];
    error?: string;
    reviewed_at?: number;
    /** What changed since the review, when it is stale: `the title`, `Approach edited`. */
    changes?: string[] | null;
  } | null;
  implement_blocker: string | null;
  /** The conversation building it, while it is in progress. */
  builder: Run | null;
  /** What happened to it, oldest first. */
  timeline: Moment[];
  settings: NodeSettings;
  /** A feature's eager build, or a task's place in one; null when eager mode doesn't apply. */
  eager: EagerFeature | EagerTaskInfo | null;
}

// --- eager mode (src/apm/core/eager.py) ---

export type EagerState = "building" | "splitting" | "split" | "failed" | "split_failed";

/** One task's part of an eager build: its branch in the chain and its pull request. */
export interface EagerTask {
  id: string;
  handle: string;
  title: string;
  status: Status;
  /** From 0: its branch is built on the one before, and its pull request targets it. */
  position: number;
  branch: string;
  commit: string;
  /** Roughly how many lines of source its part adds. */
  lines: number | null;
  pr: string | null;
  /** `stale`: a task before it changed, and it is to be rebuilt. */
  state: "open" | "merged" | "stale";
}

export interface EagerBuild {
  round: number;
  branch: string;
  base: string;
  base_sha: string;
  sha: string | null;
  state: EagerState;
  run_id: string | null;
  error: string | null;
  warnings: string[];
  tasks: string[];
  updated: number;
}

/** A feature's eager build, for its details. */
export interface EagerFeature {
  applies: boolean;
  /** `automation.eager_redo`: whether a change is rebuilt on its own. */
  auto_redo: boolean;
  build: EagerBuild | null;
  tasks: EagerTask[];
  /** A task pull request that changed since the split, or why APM can't rebuild after it. */
  pending: { node?: NodeRef; kind?: "pushed" | "merged"; blocked: string | null } | null;
  /** What Redo would do now, or (action null) why it can't. */
  redo: { action: "split" | "rebuild" | null; why: string };
}

/** A task's place in an eager feature. */
export interface EagerTaskInfo {
  feature: NodeRef | null;
  in_split: boolean;
  /** Why Implement can't start on it alone. */
  blocker: string | null;
  /** Implement builds its whole feature eagerly. */
  redirect: boolean;
}

// --- settings (apm.yaml; src/apm/core/settings.py) ---

export type ReportDetail = "brief" | "standard" | "detailed";

/** What `features:` sets, project-wide or for one feature. */
export interface FeatureSettings {
  eager: boolean;
  task_max_lines: number;
  report: boolean;
  report_detail: ReportDetail;
}

export type SettingName = keyof FeatureSettings;

/** Settings with where each comes from: the feature whose override sets it, or null for the project. */
export interface ResolvedSettings {
  values: FeatureSettings;
  sources: Record<SettingName, NodeRef | null>;
}

/** What applies to a node: its own settings on top of what it inherits. */
export interface NodeSettings extends ResolvedSettings {
  /** Only the settings the node sets itself. */
  own: Partial<FeatureSettings>;
  /** What it would have without its own settings. */
  inherited: ResolvedSettings;
}

/** What `apm serve` does on its own (`automation:`). */
export interface Automation {
  task_review: boolean;
  reconcile: boolean;
  reconcile_interval_s: number;
  feature_review: boolean;
  accept_follow_ups: boolean;
  implement_follow_ups: boolean;
  feature_pr: boolean;
  eager_redo: boolean;
}

export interface ProjectSettings {
  features: FeatureSettings;
  automation: Automation;
  defaults: { features: FeatureSettings; automation: Automation };
  /** Each feature's own settings, by its key in apm.yaml. `node` is null if the key names no node. */
  overrides: { key: string; node: string | null; values: Partial<FeatureSettings> }[];
  problems: string[];
}

/** A change to settings: null puts one back to its default, or (for a feature) inherits it again. */
export type SettingsChange<T> = { [K in keyof T]?: T[K] | null };

/** What Review all tasks or Implement all did. */
export interface Batch {
  started: Run[];
  /** Queued until there is room under runner.max_concurrent. */
  waiting: NodeRef[];
  skipped: (NodeRef & { reason: string })[];
}

// --- the feature report (src/apm/core/report_branch.py) ---

/** A report APM wrote, committed to the feature branch (or kept, with why it wasn't). */
export interface FeatureReport {
  node_id: string;
  run_id: string | null;
  branch: string;
  path: string;
  /** The commit that put it on the branch; null if it couldn't be committed. */
  sha: string | null;
  error: string | null;
  markdown: string;
  summary: string;
  detail: ReportDetail;
  created: number;
}

/** Where a feature's report stands. */
export interface ReportStatus {
  feature: string;
  state: "none" | "writing" | "written" | "not_committed";
  /** The latest report, even while a new one is being written. */
  report: FeatureReport | null;
  running: Run | null;
  /** Its page on GitHub, once committed. */
  url: string | null;
  pr: string | null;
  /** Whether it is written on its own once the feature's review passes (`features.report`). */
  automatic: boolean;
  detail: ReportDetail;
}

export interface Agent {
  name: string;
  label: string;
  summary: string;
  needs_node: boolean;
}

export interface Mode {
  name: string;
  label: string;
}

/** Something a running agent is waiting on you for. */
export interface AgentRequest {
  id: string;
  run_id: string;
  tool: string;
  input: Record<string, unknown>;
  description: string;
  kind: "permission" | "question" | "plan";
}

export interface Question {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options: { label: string; description?: string }[];
}

export interface Project {
  name: string;
  version: string;
  code_dir: string;
  head: string | null;
  agents: Agent[];
  modes: Mode[];
  default_mode: string;
  counts: { nodes: number; ready: number; proposals: number; running: number };
}

export type LiveEvent =
  | { type: "hello"; head: string | null }
  | { type: "graph.changed"; head: string }
  | { type: "run.started"; run: Run }
  | { type: "run.event"; run_id: string; event: RunEvent }
  | { type: "run.finished"; run: Run }
  | { type: "run.request"; request: AgentRequest }
  | { type: "run.request_resolved"; run_id: string; id: string }
  | { type: "proposal.created"; proposal: Proposal }
  | { type: "proposal.resolved"; id: string; status: string }
  | { type: "report.written"; node_id: string };
