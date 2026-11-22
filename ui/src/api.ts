// Talking to `apm serve`. Every request carries the token from the URL the server printed.

import type { Automation, Batch, FeatureSettings, NodeSettings, ProjectSettings, Run, SettingsChange } from "./types";

const TOKEN_KEY = "apm-token";

/** Takes the token out of the address bar on first load and remembers it for this tab. */
export function takeToken(): string | null {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl) {
    sessionStorage.setItem(TOKEN_KEY, fromUrl);
    url.searchParams.delete("token");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  return sessionStorage.getItem(TOKEN_KEY);
}

export function token(): string {
  return sessionStorage.getItem(TOKEN_KEY) ?? "";
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function send(path: string, init: { method?: string; body?: unknown } = {}): Promise<Response> {
  const response = await fetch(path, {
    method: init.method ?? "GET",
    headers: {
      Authorization: `Bearer ${token()}`,
      ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (typeof data.error === "string") message = data.error;
      else if (Array.isArray(data.detail)) message = data.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      // not JSON: keep the status line
    }
    throw new ApiError(response.status, message);
  }
  return response;
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  return (await (await send(path, init)).json()) as T;
}

/** The file name a response's Content-Disposition header gives, or null. */
function attachmentName(header: string | null): string | null {
  if (!header) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1]!);
    } catch {
      // malformed: fall back to the plain name
    }
  }
  return /filename="([^"]+)"/i.exec(header)?.[1] ?? null;
}

/**
 * Downloads a file from the API and saves it. A plain link can't do this: the token has to go in
 * a header, so the file is fetched here and handed to the browser as a Blob.
 */
export async function download(path: string, fallbackName: string): Promise<string> {
  const response = await send(path);
  const name = attachmentName(response.headers.get("Content-Disposition")) ?? fallbackName;
  const url = URL.createObjectURL(await response.blob());
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    // Let the browser start the save before the Blob goes away.
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
  return name;
}

export function query(params: Record<string, string | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

// --- settings and feature-level actions ---

export function getSettings(): Promise<ProjectSettings> {
  return api<ProjectSettings>("/api/settings");
}

/** Changes the project's settings in apm.yaml, keeping its comments. A bad value is refused. */
export function saveSettings(change: {
  features?: SettingsChange<FeatureSettings>;
  automation?: SettingsChange<Automation>;
}): Promise<ProjectSettings> {
  return api<ProjectSettings>("/api/settings", { method: "PATCH", body: change });
}

/** Changes a feature's own settings: null inherits that one again. */
export function saveNodeSettings(id: string, change: SettingsChange<FeatureSettings>): Promise<NodeSettings> {
  return api<NodeSettings>(`/api/nodes/${id}/settings`, { method: "PATCH", body: change });
}

export function clearNodeSettings(id: string): Promise<NodeSettings> {
  return api<NodeSettings>(`/api/nodes/${id}/settings`, { method: "DELETE" });
}

/** Review all tasks: a task review on every To do task inside the feature. */
export function reviewAllTasks(id: string): Promise<Batch> {
  return api<Batch>(`/api/nodes/${id}/review-tasks`, { method: "POST", body: {} });
}

/** Implement all: the implementer on every ready task inside the feature that passed its review. */
export function implementAll(id: string): Promise<Batch> {
  return api<Batch>(`/api/nodes/${id}/implement`, { method: "POST", body: {} });
}

/** Redo on an eager feature: rebuild the tasks after one that changed, or split again. */
export function redoEager(id: string): Promise<Run> {
  return api<Run>(`/api/nodes/${id}/eager/redo`, { method: "POST", body: {} });
}
