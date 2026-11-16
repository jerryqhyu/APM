import type { NodeView, RunStatus } from "./types";

export type StateKey = "ready" | "blocked" | "todo" | "in_progress" | "done" | "dropped";

/** One word for where a node stands: the stored status, refined by what's computed. */
export function stateOf(node: Pick<NodeView, "status" | "ready" | "blocked">): { key: StateKey; label: string } {
  if (node.status === "todo") {
    if (node.blocked) return { key: "blocked", label: "Blocked" };
    if (node.ready) return { key: "ready", label: "Ready" };
    return { key: "todo", label: "To do" };
  }
  if (node.status === "in_progress") return { key: "in_progress", label: "In progress" };
  if (node.status === "done") return { key: "done", label: "Done" };
  return { key: "dropped", label: "Dropped" };
}

export const STATUS_LABEL = { todo: "To do", in_progress: "In progress", done: "Done", dropped: "Dropped" } as const;

export const RUN_LABEL: Record<RunStatus, string> = {
  running: "Running",
  succeeded: "Finished",
  failed: "Failed",
  cancelled: "Stopped",
};

export const AGENT_LABEL: Record<string, string> = {
  "apm-decomposer": "Decompose",
  "apm-critic": "Review",
  "apm-implementer": "Implement",
  "apm-assistant": "Ask",
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

export function duration(start: number, end: number | null): string {
  const seconds = Math.max(0, Math.round((end ?? Date.now() / 1000) - start));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function money(usd: number | null): string {
  return usd === null ? "" : `$${usd.toFixed(2)}`;
}
