import type { NodeView, Run, RunStatus, StackedOn } from "./types";

export type StateKey = "ready" | "stacked" | "blocked" | "todo" | "in_progress" | "done" | "dropped";

/** `#20, 7d2e0f11`: what a stacked node builds on, by pull request where it has one. */
export function stackedNames(stacked: StackedOn[]): string {
  return stacked.map((s) => s.pr ?? s.handle).join(", ");
}

/** One word for where a node stands: the stored status, refined by what's computed. */
export function stateOf(
  node: Pick<NodeView, "status" | "ready" | "blocked"> & { stacked_on?: StackedOn[] },
): { key: StateKey; label: string } {
  if (node.status === "todo") {
    if (node.blocked) return { key: "blocked", label: "Blocked" };
    if (node.ready && node.stacked_on?.length) {
      return { key: "stacked", label: `Ready · stacked on ${stackedNames(node.stacked_on)}` };
    }
    if (node.ready) return { key: "ready", label: "Ready" };
    return { key: "todo", label: "To do" };
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
