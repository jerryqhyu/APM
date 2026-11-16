// What an agent said and did during one run, updating live while it is still going.

import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useLiveEvents } from "./live";
import { Markdown } from "./Markdown";
import type { Run, RunEvent } from "./types";
import { Spinner } from "./ui";
import { money } from "./util";

interface Block {
  type: string;
  text?: string;
  name?: string;
  input?: unknown;
  content?: unknown;
  is_error?: boolean;
}

function blocksOf(event: RunEvent): Block[] {
  const message = event.data.message as { content?: unknown } | undefined;
  return Array.isArray(message?.content) ? (message.content as Block[]) : [];
}

function oneLine(value: unknown, limit = 140): string {
  let text: string;
  if (typeof value === "string") text = value;
  else if (Array.isArray(value)) {
    text = value.map((v) => (typeof v === "object" && v && "text" in v ? String(v.text) : JSON.stringify(v))).join(" ");
  } else text = JSON.stringify(value) ?? "";
  text = text.replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function full(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value.map((v) => (typeof v === "object" && v && "text" in v ? String(v.text) : JSON.stringify(v, null, 2))).join("\n");
  }
  return JSON.stringify(value, null, 2) ?? "";
}

function toolName(name: string | undefined): string {
  return (name ?? "tool").replace(/^mcp__apm__/, "");
}

function EventView({ event }: { event: RunEvent }) {
  const kind = event.data.type;
  if (kind === "assistant") {
    return (
      <>
        {blocksOf(event).map((block, i) => {
          if (block.type === "text" && block.text?.trim()) {
            return <Markdown key={i} text={block.text} className="said" />;
          }
          if (block.type === "tool_use") {
            // The agent's final structured answer is shown as the proposal, not as a tool call.
            if (block.name === "StructuredOutput") {
              return (
                <div key={i} className="tool answered">
                  ✓ Returned its answer
                </div>
              );
            }
            return (
              <details key={i} className="tool">
                <summary>
                  <span className="tool-name">{toolName(block.name)}</span>
                  <span className="tool-input">{oneLine(block.input)}</span>
                </summary>
                <pre>{full(block.input)}</pre>
              </details>
            );
          }
          return null;
        })}
      </>
    );
  }
  if (kind === "user") {
    return (
      <>
        {blocksOf(event).map((block, i) =>
          block.type === "tool_result" && block.is_error ? (
            <details key={i} className="tool failed">
              <summary>
                <span className="tool-name">refused or failed</span>
                <span className="tool-input">{oneLine(block.content)}</span>
              </summary>
              <pre>{full(block.content)}</pre>
            </details>
          ) : null,
        )}
      </>
    );
  }
  return null;
}

export function Transcript({ run }: { run: Run }) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loaded, setLoaded] = useState(false);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setEvents([]);
    setLoaded(false);
    void api<Run & { events: RunEvent[] }>(`/api/runs/${run.id}`).then((data) => {
      if (cancelled) return;
      // Live events may have arrived while this was loading: keep whichever are newer.
      setEvents((live) => {
        const last = data.events.at(-1)?.seq ?? 0;
        return [...data.events, ...live.filter((e) => e.seq > last)];
      });
      setLoaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, [run.id]);

  useLiveEvents((event) => {
    if (event.type === "run.event" && event.run_id === run.id) {
      setEvents((before) => (before.some((e) => e.seq === event.event.seq) ? before : [...before, event.event]));
    }
  });

  useEffect(() => {
    if (run.status === "running") end.current?.scrollIntoView({ block: "nearest" });
  }, [events.length, run.status]);

  return (
    <div className="transcript">
      <Asked run={run} />
      {events.map((event) => (
        <EventView key={event.seq} event={event} />
      ))}
      {!loaded && <p className="empty">Loading…</p>}
      {run.status === "running" && (
        <div className="working">
          <Spinner /> Working…
        </div>
      )}
      {run.status === "failed" && <div className="run-error">Failed: {run.error ?? "no reason given"}</div>}
      {run.status === "cancelled" && <div className="run-error muted">Stopped before it finished.</div>}
      {run.status === "succeeded" && <div className="run-done">Finished {run.cost_usd !== null && `· ${money(run.cost_usd)}`}</div>}
      <div ref={end} />
    </div>
  );
}

/** The prompt that started the run. Long ones (a reviewer is handed the whole proposal) fold. */
function Asked({ run }: { run: Run }) {
  const [open, setOpen] = useState(false);
  const automatic = run.agent === "apm-critic" || run.prompt.startsWith("A reviewer looked at your breakdown");
  const long = run.prompt.length > 420;
  return (
    <div className="asked">
      <div className="label">{automatic ? "APM asked" : "You asked"}</div>
      <div className="asked-text">{long && !open ? `${run.prompt.slice(0, 280).trimEnd()}…` : run.prompt}</div>
      {long && (
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show all"}
        </button>
      )}
    </div>
  );
}
