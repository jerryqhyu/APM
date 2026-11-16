// The shapes the server returns (src/apm/core/views.py and src/apm/server/app.py).

export type Status = "todo" | "in_progress" | "done" | "dropped";
export type Kind = "work" | "decision";

export interface Delivery {
  repo: string;
  branch: string | null;
  commits: string[];
  pr: string | null;
}

export interface NodeView {
  id: string;
  handle: string;
  parent: string | null;
  title: string;
  kind: Kind;
  status: Status;
  blocked: boolean;
  ready: boolean;
  waiting_on: string[];
  dropped_deps: string[];
  depends_on: string[];
  dependents: string[];
  children: number;
  progress: { done: number; total: number };
  delivery: Delivery | null;
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
  acceptance: string[];
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
  data: { rationale: string; children: ProposedChild[]; warnings: string[] };
  review: { verdict: string; summary: string; issues: ReviewIssue[]; revised?: boolean } | null;
  status: "pending" | "accepted" | "rejected";
  created: number;
}

export interface NodeDetail extends NodeView {
  body: string;
  ancestors: NodeRef[];
  links: { depends_on: NodeRef[]; dependents: NodeRef[]; waiting_on: NodeRef[] };
  history: HistoryEntry[];
  runs: Run[];
  proposals: Proposal[];
}

export interface Agent {
  name: string;
  summary: string;
}

export interface Project {
  name: string;
  version: string;
  code_dir: string;
  head: string | null;
  agents: Agent[];
  counts: { nodes: number; ready: number; proposals: number; running: number };
}

export type LiveEvent =
  | { type: "hello"; head: string | null }
  | { type: "graph.changed"; head: string }
  | { type: "run.started"; run: Run }
  | { type: "run.event"; run_id: string; event: RunEvent }
  | { type: "run.finished"; run: Run }
  | { type: "proposal.created"; proposal: Proposal }
  | { type: "proposal.resolved"; id: string; status: string };
