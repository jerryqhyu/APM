import type { Gate, NodeView, Proposal, Run, RunStatus, StackedOn } from "./types";

export type StateKey =
  | "ready"
  | "stacked"
  | "unreviewed"
  | "blocked"
  | "todo"
  | "in_progress"
  | "review"
  | "done"
  | "dropped";

/** What a ready task whose review holds it back is called. */
export const GATE_LABEL: Record<Gate, string> = {
  breakdown: "Not broken down",
  unreviewed: "Not reviewed",
  error: "Not reviewed",
  too_big: "Too big",
  unclear: "Unclear",
};

/** `#20`: what a stacked node builds on (its stack top), by pull request where it has one. */
export function stackedName(node: { stacked_on?: StackedOn[]; stack_top?: string | null }): string {
  const top = node.stacked_on?.find((s) => s.id === node.stack_top);
  return top ? (top.pr ?? top.handle) : "";
}

/** One word for where a node stands: the stored status, refined by what's computed. */
export function stateOf(
  node: Pick<NodeView, "status" | "ready" | "blocked"> & {
    stacked_on?: StackedOn[];
    stack_top?: string | null;
    delivery?: NodeView["delivery"];
    children?: number;
    gate?: Gate | null;
  },
): { key: StateKey; label: string } {
  if (node.status === "todo") {
    if (node.blocked) return { key: "blocked", label: "Blocked" };
    // Nothing in its way but its review: it can't be built until a review of it passes.
    if (node.ready && node.gate) return { key: "unreviewed", label: GATE_LABEL[node.gate] };
    if (node.ready && node.stack_top) return { key: "stacked", label: `Ready · stacked on ${stackedName(node)}` };
    if (node.ready) return { key: "ready", label: "Ready" };
    return { key: "todo", label: "To do" };
  }
  // Built and delivered, not merged yet: it waits for you to review its pull request.
  if (node.status === "in_progress" && !node.children && (node.delivery?.pr || node.delivery?.branch)) {
    return { key: "review", label: "Pending PR review" };
  }
  if (node.status === "in_progress") return { key: "in_progress", label: "In progress" };
  if (node.status === "done") return { key: "done", label: "Done" };
  return { key: "dropped", label: "Dropped" };
}

export const STATUS_LABEL = { todo: "To do", in_progress: "In progress", done: "Done", dropped: "Dropped" } as const;

export const RUN_LABEL: Record<RunStatus, string> = {
  running: "Working",
  succeeded: "Finished",
  failed: "Failed",
  cancelled: "Stopped",
};

export const AGENT_LABEL: Record<string, string> = {
  "apm-assistant": "Chat",
  "apm-decomposer": "Decompose",
  "apm-critic": "Review",
  "apm-implementer": "Implement",
  "apm-task-reviewer": "Review task",
  "apm-feature-reviewer": "Review feature",
  "apm-feature-reporter": "Write report",
  "apm-eager-builder": "Eager build",
  "apm-splitter": "Split",
};

export function cx(...names: (string | false | null | undefined)[]): string {
  return names.filter(Boolean).join(" ");
}

export function timeAgo(seconds: number): string {
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)} min ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} h ago`;
  if (delta < 86400 * 7) return `${Math.floor(delta / 86400)} d ago`;
  return new Date(seconds * 1000).toLocaleDateString();
}

export function money(usd: number | null): string {
  return usd === null ? "" : `$${usd.toFixed(2)}`;
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** A conversation: the runs that follow one another, oldest first. */
export interface Thread {
  id: string;
  runs: Run[];
  agent: string;
  title: string;
  updated: number;
  running: boolean;
  cost: number;
}

/**
 * Groups runs into conversations. A run belongs to the conversation of the run it follows
 * (`parent_run`), so a decomposer, its reviewer and its revision read as one thread.
 */
export function threadsOf(runs: Run[]): Thread[] {
  const byId = new Map(runs.map((r) => [r.id, r]));
  const rootOf = (run: Run): Run => {
    let current = run;
    const seen = new Set<string>();
    while (current.parent_run && byId.has(current.parent_run) && !seen.has(current.id)) {
      seen.add(current.id);
      current = byId.get(current.parent_run)!;
    }
    return current;
  };
  const groups = new Map<string, Run[]>();
  for (const run of runs) {
    const root = rootOf(run).id;
    groups.set(root, [...(groups.get(root) ?? []), run]);
  }
  const threads: Thread[] = [];
  for (const [id, members] of groups) {
    members.sort((a, b) => a.started - b.started);
    const first = members[0]!;
    threads.push({
      id,
      runs: members,
      agent: first.agent,
      title: threadTitle(first),
      updated: Math.max(...members.map((r) => r.ended ?? r.started)),
      running: members.some((r) => r.status === "running"),
      cost: members.reduce((sum, r) => sum + (r.cost_usd ?? 0), 0),
    });
  }
  return threads.sort((a, b) => b.updated - a.updated);
}

/**
 * What you typed, without what APM adds around it: a chat started on a node opens with
 * "The user is looking at …" before your words.
 */
export function typedText(run: Run): string {
  if (run.agent === "apm-assistant" && run.prompt.startsWith("The user is looking at ") && run.prompt.includes("\n\n")) {
    return run.prompt.slice(run.prompt.indexOf("\n\n") + 2);
  }
  return run.prompt;
}

function threadTitle(first: Run): string {
  if (first.agent !== "apm-assistant") {
    const when = new Date(first.started * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    return `${AGENT_LABEL[first.agent] ?? first.agent} · ${when}`;
  }
  const line = typedText(first).trim().split("\n")[0] ?? "";
  return line.length > 44 ? `${line.slice(0, 43)}…` : line || "Chat";
}

/** A task review's split: accepted, its parts replace the task in its feature, rather than going
 * inside it as a breakdown's children do.
 */
export const isSplit = (proposal: Proposal): boolean => proposal.split ?? !!proposal.data.split_of;

/** What a proposal offers, for a callout or a toast. */
export function proposalSummary(proposal: Proposal, title: string): string {
  const count = proposal.data.children.length;
  return isSplit(proposal)
    ? `Claude proposes splitting “${title}” into ${count} tasks.`
    : `Claude proposed ${count} children for “${title}”.`;
}
