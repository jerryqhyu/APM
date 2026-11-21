// The conversation pane: talk to Claude about the plan or one node, turn by turn. Every agent run
// shows up here, so this is also where you watch a decomposition or an implementation, answer
// what Claude asks, and approve or refuse what it wants to do.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, query } from "./api";
import { useLiveEvents } from "./live";
import { Markdown } from "./Markdown";
import type { AgentRequest, Mode, Question, Run, RunEvent } from "./types";
import { Spinner } from "./ui";
import { AGENT_LABEL, type Thread, cx, errorText, money, threadsOf, timeAgo, typedText } from "./util";

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

function textOf(value: unknown, pretty = false): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((v) => (typeof v === "object" && v && "text" in v ? String(v.text) : JSON.stringify(v, null, pretty ? 2 : 0)))
      .join(pretty ? "\n" : " ");
  }
  return JSON.stringify(value, null, pretty ? 2 : 0) ?? "";
}

function oneLine(value: unknown, limit = 120): string {
  const text = textOf(value).replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function toolName(name: string | undefined): string {
  return (name ?? "tool").replace(/^mcp__apm__/, "");
}

/** What the agent did with a tool, in a few plain words. */
function describeTool(block: Block): string {
  const input = (block.input ?? {}) as Record<string, unknown>;
  const pick = (key: string) => (typeof input[key] === "string" ? (input[key] as string) : "");
  return pick("description") || pick("command") || pick("file_path") || pick("pattern") || pick("query") || oneLine(input);
}

// --- one request: a permission prompt, a question, or a plan to approve ---

function RequestCard({ request, onError }: { request: AgentRequest; onError: (text: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const questions = (request.input.questions as Question[] | undefined) ?? [];
  const [answers, setAnswers] = useState<Record<string, string>>({});

  const send = async (body: { allow: boolean; message?: string; answers?: Record<string, string> }) => {
    setBusy(true);
    try {
      await api(`/api/requests/${request.id}`, { method: "POST", body });
    } catch (err) {
      onError(errorText(err));
      setBusy(false);
    }
  };

  if (request.kind === "question") {
    const complete = questions.every((q) => answers[q.question]?.trim());
    // A multi-select question takes several options, sent as one comma-separated answer.
    const picked = (q: Question) => (answers[q.question] ?? "").split(", ").filter(Boolean);
    const isPicked = (q: Question, label: string) => (q.multiSelect ? picked(q).includes(label) : answers[q.question] === label);
    const pick = (q: Question, label: string) => {
      if (!q.multiSelect) return setAnswers({ ...answers, [q.question]: label });
      const now = picked(q).filter((l) => q.options.some((o) => o.label === l));
      const next = now.includes(label) ? now.filter((l) => l !== label) : [...now, label];
      setAnswers({ ...answers, [q.question]: q.options.map((o) => o.label).filter((l) => next.includes(l)).join(", ") });
    };
    const ownWords = (q: Question) =>
      q.multiSelect ? picked(q).every((l) => q.options.some((o) => o.label === l)) : q.options.some((o) => o.label === answers[q.question]);
    return (
      <div className="request question">
        <div className="request-head">Claude is asking</div>
        {questions.map((q) => (
          <div key={q.question} className="question-block">
            <p className="question-text">
              {q.question}
              {q.multiSelect && <span className="tool-tag">pick any</span>}
            </p>
            <div className="options">
              {q.options.map((option) => (
                <button
                  key={option.label}
                  className={cx("option", isPicked(q, option.label) && "chosen")}
                  aria-pressed={isPicked(q, option.label)}
                  title={option.description}
                  disabled={busy}
                  onClick={() => pick(q, option.label)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <input
              className="own-answer"
              placeholder="Or answer in your own words"
              disabled={busy}
              value={ownWords(q) ? "" : (answers[q.question] ?? "")}
              onChange={(e) => setAnswers({ ...answers, [q.question]: e.target.value })}
            />
          </div>
        ))}
        <div className="request-actions">
          <button className="primary" disabled={busy || !complete} onClick={() => void send({ allow: true, answers })}>
            Answer
          </button>
          <button disabled={busy} onClick={() => void send({ allow: false, message: "The user skipped the question." })}>
            Skip
          </button>
        </div>
      </div>
    );
  }

  const isPlan = request.kind === "plan";
  const command = typeof request.input.command === "string" ? request.input.command : null;
  const plan = typeof request.input.plan === "string" ? request.input.plan : null;
  return (
    <div className="request permission">
      <div className="request-head">{isPlan ? "Claude has a plan and waits for your go-ahead" : "Claude asks permission"}</div>
      {isPlan && plan ? (
        <Markdown text={plan} />
      ) : (
        <>
          <p className="question-text">
            {request.description || `Use ${toolName(request.tool)}`}
            <span className="tool-tag">{toolName(request.tool)}</span>
          </p>
          <pre className="request-input">{command ?? textOf(request.input, true)}</pre>
        </>
      )}
      <div className="request-actions">
        <button className="primary" disabled={busy} onClick={() => void send({ allow: true })}>
          {isPlan ? "Approve the plan" : "Allow"}
        </button>
        <input
          placeholder={isPlan ? "What to change (optional)" : "Why not (optional): Claude sees this"}
          value={reason}
          disabled={busy}
          onChange={(e) => setReason(e.target.value)}
        />
        <button disabled={busy} onClick={() => void send({ allow: false, message: reason })}>
          {isPlan ? "Keep planning" : "Deny"}
        </button>
      </div>
    </div>
  );
}

// --- one turn: your message, then what the agent said and did ---

function Turn({
  run,
  first,
  onError,
  onGrow,
}: {
  run: Run;
  first: boolean;
  onError: (text: string) => void;
  /** Called when the turn gets longer, so the pane can keep its end in view. */
  onGrow: () => void;
}) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void api<Run & { events: RunEvent[] }>(`/api/runs/${run.id}`)
      .then((data) => {
        if (cancelled) return;
        // Live events may have arrived while this was loading: keep whichever are newer.
        setEvents((live) => {
          const last = data.events.at(-1)?.seq ?? 0;
          return [...data.events, ...live.filter((e) => e.seq > last)];
        });
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
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
    if (events.length) onGrow();
    // Only when something was added.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.length]);

  const answered = useMemo(() => {
    const map = new Map<string, { allow: boolean; message: string; answers: Record<string, string> }>();
    for (const event of events) {
      if (event.data.type === "apm_request_resolved") map.set(String(event.data.id), event.data as never);
    }
    return map;
  }, [events]);

  const automatic = run.agent === "apm-critic" || run.prompt.startsWith("A reviewer looked at your breakdown");
  const label = automatic ? "APM" : "You";
  return (
    <div className="turn">
      <Said who={label} agent={first || automatic ? run.agent : null} text={typedText(run)} />
      {events.map((event) => {
        const kind = event.data.type;
        if (kind === "assistant") {
          return blocksOf(event).map((block, i) => {
            if (block.type === "text" && block.text?.trim()) {
              return <Markdown key={`${event.seq}-${i}`} text={block.text} className="claude-said" />;
            }
            if (block.type === "tool_use" && block.name !== "AskUserQuestion" && block.name !== "ExitPlanMode") {
              if (block.name === "StructuredOutput") {
                return (
                  <div key={`${event.seq}-${i}`} className="did answered">
                    Returned its answer
                  </div>
                );
              }
              return (
                <details key={`${event.seq}-${i}`} className="did">
                  <summary>
                    <span className="tool-name">{toolName(block.name)}</span>
                    <span className="tool-what">{describeTool(block)}</span>
                  </summary>
                  <pre>{textOf(block.input, true)}</pre>
                </details>
              );
            }
            return null;
          });
        }
        if (kind === "user") {
          return blocksOf(event).map((block, i) =>
            block.type === "tool_result" && block.is_error ? (
              <details key={`${event.seq}-${i}`} className="did failed">
                <summary>
                  <span className="tool-name">did not work</span>
                  <span className="tool-what">{oneLine(block.content)}</span>
                </summary>
                <pre>{textOf(block.content, true)}</pre>
              </details>
            ) : null,
          );
        }
        if (kind === "apm_request") {
          const request = event.data as unknown as AgentRequest;
          const outcome = answered.get(request.id);
          if (!outcome && run.status === "running") {
            return <RequestCard key={event.seq} request={request} onError={onError} />;
          }
          return (
            <div key={event.seq} className={cx("did", "decided", outcome && !outcome.allow && "refused")}>
              {request.kind === "question"
                ? `You answered: ${Object.values(outcome?.answers ?? {}).join(", ") || "(skipped)"}`
                : `${outcome?.allow ? "You allowed" : outcome ? "You refused" : "Unanswered"}: ${
                    request.description || toolName(request.tool)
                  }${outcome && !outcome.allow && outcome.message ? ` (“${outcome.message}”)` : ""}`}
            </div>
          );
        }
        if (kind === "apm_notice") {
          // A line from APM itself, after the agent's turn: e.g. the pull request it opened.
          const level = String(event.data.level ?? "info");
          return (
            <div key={event.seq} className={cx("apm-notice", level === "warning" && "warn")}>
              <span className="apm-notice-who">APM</span>
              <Markdown text={String(event.data.text ?? "")} />
            </div>
          );
        }
        return null;
      })}
      {!loaded && <p className="empty">Loading…</p>}
      {run.status === "failed" && <div className="turn-error">This didn’t finish: {run.error ?? "no reason given"}</div>}
      {run.status === "cancelled" && <div className="turn-note">Stopped before it finished.</div>}
    </div>
  );
}

function Said({ who, agent, text }: { who: string; agent: string | null; text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 420;
  return (
    <div className="you-said">
      <div className="byline">
        {who}
        {agent && agent !== "apm-assistant" && <span className="agent-tag">{AGENT_LABEL[agent] ?? agent}</span>}
      </div>
      <div className="you-text">{long && !open ? `${text.slice(0, 260).trimEnd()}…` : text}</div>
      {long && (
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show all"}
        </button>
      )}
    </div>
  );
}

// --- the pane ---

export interface ChatContext {
  /** The node the conversation is about, or null for the whole plan. */
  nodeId: string | null;
  title: string;
}

export function Chat({
  context,
  modes,
  defaultMode,
  focus,
  onError,
  onChanged,
}: {
  context: ChatContext;
  modes: Mode[];
  defaultMode: string;
  /** A run to bring into view, e.g. one just started from a button. */
  focus: string | null;
  onError: (text: string) => void;
  onChanged: () => void;
}) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [picked, setPicked] = useState<string | "new" | null>(null);
  const [message, setMessage] = useState("");
  // The mode you picked by hand, if any. Otherwise a conversation keeps the mode it has, and a new
  // one starts in the project's default.
  const [modeChoice, setModeChoice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Runs that are waiting on an answer from you.
  const [requestsWaiting, setRequestsWaiting] = useState<Set<string>>(new Set());
  const end = useRef<HTMLDivElement>(null);
  const key = context.nodeId ?? "project";

  const load = useCallback(async () => {
    const where = context.nodeId ? query({ node_id: context.nodeId }) : query({ scope: "project" });
    setRuns(await api<Run[]>(`/api/runs${where}`));
  }, [context.nodeId]);

  useEffect(() => {
    setRuns([]);
    setPicked(null);
    setMessage("");
    setModeChoice(null);
    load().catch((err) => onError(errorText(err)));
    // Reload when you move to another node, not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useLiveEvents((event) => {
    if (event.type === "run.started" || event.type === "run.finished") {
      const about = event.run.node_id || null;
      if (about === context.nodeId) void load();
    }
    if (event.type === "run.request") setRequestsWaiting((before) => new Set(before).add(event.request.run_id));
    if (event.type === "run.request_resolved" || event.type === "run.finished") {
      const id = event.type === "run.finished" ? event.run.id : event.run_id;
      setRequestsWaiting((before) => {
        const after = new Set(before);
        after.delete(id);
        return after;
      });
    }
  });

  const threads = useMemo(() => threadsOf(runs), [runs]);
  const focused = focus ? threads.find((t) => t.runs.some((r) => r.id === focus)) : undefined;
  const thread: Thread | null =
    picked === "new" ? null : (threads.find((t) => t.id === picked) ?? focused ?? threads[0] ?? null);
  const running = thread?.runs.find((r) => r.status === "running") ?? null;
  // A follow-up goes to the last turn you could talk to: not the automatic reviewer.
  const last = thread ? [...thread.runs].reverse().find((r) => r.agent !== "apm-critic") : undefined;

  useEffect(() => {
    if (focus) setPicked(null);
  }, [focus]);
  const mode = modeChoice ?? (last?.mode || defaultMode);
  useEffect(() => setModeChoice(null), [thread?.id]);
  const toEnd = useCallback(() => end.current?.scrollIntoView({ block: "end" }), []);
  useEffect(() => {
    if (thread) toEnd();
  }, [thread?.id, thread?.runs.length, running?.id, thread, toEnd]);
  const waiting = running !== null && requestsWaiting.has(running.id);

  const send = async () => {
    const text = message.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      const run = last
        ? await api<Run>(`/api/runs/${last.id}/continue`, { method: "POST", body: { message: text, mode } })
        : await api<Run>("/api/runs", {
            method: "POST",
            body: { agent: "apm-assistant", node_id: context.nodeId, message: text, mode },
          });
      setMessage("");
      setPicked(null);
      await load();
      // A new conversation becomes the one in view.
      if (!last) setPicked(run.id);
      onChanged();
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (running) await api(`/api/runs/${running.id}`, { method: "DELETE" }).catch((err) => onError(errorText(err)));
  };

  return (
    <section className="chat" aria-label="Conversation">
      <div className="threads" role="tablist" aria-label="Conversations">
        <button
          role="tab"
          aria-selected={thread === null}
          className={cx("thread-chip", "new", thread === null && "active")}
          onClick={() => setPicked("new")}
        >
          + New
        </button>
        {threads.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={thread?.id === t.id}
            className={cx("thread-chip", thread?.id === t.id && "active")}
            title={`${AGENT_LABEL[t.agent] ?? t.agent} · ${timeAgo(t.updated)}${t.cost ? ` · ${money(t.cost)}` : ""}`}
            onClick={() => setPicked(t.id)}
          >
            {t.running && <Spinner />}
            <span>{t.title}</span>
          </button>
        ))}
      </div>

      <div className="chat-scroll">
        {thread ? (
          <>
            {thread.runs.map((run, i) => (
              <Turn key={run.id} run={run} first={i === 0} onError={onError} onGrow={toEnd} />
            ))}
            {running && !waiting && (
              <div className="working">
                <Spinner /> Claude is working…
              </div>
            )}
            {!running && thread.cost > 0 && <div className="turn-note">This conversation has cost {money(thread.cost)}.</div>}
          </>
        ) : (
          <div className="chat-empty">
            <h3>{context.nodeId ? `Talk about “${context.title}”` : "Talk about the plan"}</h3>
            <p>Ask where things stand, think something through, or have Claude change the plan for you.</p>
            <div className="starters">
              {(context.nodeId
                ? ["Is this well specified? What is missing?", "Break this into steps and add them as children.", "What should I do first?"]
                : ["Where does the plan stand, and what is next?", "Help me plan a new feature.", "Look for duplicate or badly ordered work."]
              ).map((starter) => (
                <button key={starter} className="starter" onClick={() => setMessage(starter)}>
                  {starter}
                </button>
              ))}
            </div>
          </div>
        )}
        <div ref={end} />
      </div>

      <div className="composer">
        <textarea
          rows={2}
          value={message}
          placeholder={
            waiting
              ? "Claude is waiting for your answer above."
              : running
                ? "Claude is working. You can stop it, or wait for its reply."
              : last
                ? "Reply…"
                : context.nodeId
                  ? "Ask or tell Claude something about this node…"
                  : "Ask or tell Claude something about the plan…"
          }
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !running) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <div className="composer-bar">
          <label className="mode" title={modes.find((m) => m.name === mode)?.label}>
            <span>Permissions</span>
            <select value={mode} onChange={(e) => setModeChoice(e.target.value)}>
              {modes.map((m) => (
                <option key={m.name} value={m.name} title={m.label}>
                  {m.label.split(":")[0]}
                </option>
              ))}
            </select>
          </label>
          <span className="spacer" />
          {running ? (
            <button className="danger" onClick={() => void stop()}>
              Stop
            </button>
          ) : (
            <button className="primary" disabled={busy || !message.trim()} onClick={() => void send()}>
              Send
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
