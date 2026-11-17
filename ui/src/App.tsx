import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type ActionHandlers, Actions } from "./Actions";
import { ApiError, api, query } from "./api";
import { Canvas } from "./Canvas";
import { Chat } from "./Chat";
import { type DetailActions, NodeDetails, PlanSummary } from "./Details";
import { AddNodeDialog, DeleteDialog, MergeDialog, PickNodeDialog, RunAgentDialog } from "./Dialogs";
import { useLiveConnection, useLiveEvents } from "./live";
import { Outline } from "./Outline";
import { ProposalReview } from "./ProposalReview";
import { type Theme, useTheme } from "./theme";
import type { AgentRequest, Level, NodeDetail, NodeView, Project, Proposal, Run, Status, Tree } from "./types";
import { Spinner } from "./ui";
import { cx, errorText } from "./util";

interface Route {
  /** The node whose inside the graph shows; null = the top level. */
  root: string | null;
  /** The selected nodes, the one clicked last at the end. */
  selected: string[];
}

type Dialog =
  | { kind: "add"; parent: string | null }
  | { kind: "run"; agent: string; node: string }
  | { kind: "move"; node: string }
  | { kind: "link"; node: string }
  | { kind: "merge"; nodes: string[] }
  | { kind: "delete"; nodes: string[] }
  | { kind: "review"; proposal: Proposal };

type Panes = "split" | "chat" | "details";

function readRoute(): Route {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const node = params.get("node");
  return { root: params.get("in"), selected: node ? [node] : [] };
}

function writeRoute(route: Route): void {
  const hash = query({ in: route.root, node: route.selected.at(-1) }).replace(/^\?/, "#");
  if (hash !== window.location.hash) window.history.pushState(null, "", hash || window.location.pathname);
}

export function App() {
  const connected = useLiveConnection();
  const [theme, setTheme] = useTheme();
  const [project, setProject] = useState<Project | null>(null);
  const [tree, setTree] = useState<Tree | null>(null);
  const [level, setLevel] = useState<Level | null>(null);
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [runningRuns, setRunningRuns] = useState<Run[]>([]);
  const [requests, setRequests] = useState<AgentRequest[]>([]);
  const [route, setRouteState] = useState<Route>(readRoute);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [panes, setPanes] = useState<Panes>("split");
  const [focusRun, setFocusRun] = useState<string | null>(null);
  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const [fatal, setFatal] = useState<string | null>(null);
  const toastId = useRef(0);

  const nodes = tree?.nodes ?? [];
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const running = useMemo(() => new Set(runningRuns.map((r) => r.node_id).filter(Boolean)), [runningRuns]);
  const proposedOn = useMemo(() => new Set(proposals.map((p) => p.node_id)), [proposals]);
  const selectedSet = useMemo(() => new Set(route.selected), [route.selected]);
  const selectedNodes = route.selected.map((id) => byId.get(id)).filter((n): n is NodeView => n !== undefined);
  const primary = route.selected.length === 1 ? route.selected[0]! : null;

  const toast = useCallback((text: string) => {
    const id = ++toastId.current;
    setToasts((before) => [...before, { id, text }]);
    window.setTimeout(() => setToasts((before) => before.filter((t) => t.id !== id)), 7000);
  }, []);

  const setRoute = useCallback((next: Route) => {
    writeRoute(next);
    setRouteState(next);
  }, []);

  useEffect(() => {
    const onChange = () => setRouteState(readRoute());
    window.addEventListener("popstate", onChange);
    window.addEventListener("hashchange", onChange);
    return () => {
      window.removeEventListener("popstate", onChange);
      window.removeEventListener("hashchange", onChange);
    };
  }, []);

  // --- loading ---

  const fail = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.status === 401) setFatal("token");
      else toast(errorText(err));
    },
    [toast],
  );

  const loadPlan = useCallback(async () => {
    const [nextProject, nextTree, nextProposals, nextRunning, nextRequests] = await Promise.all([
      api<Project>("/api/project"),
      api<Tree>("/api/tree"),
      api<Proposal[]>("/api/proposals"),
      api<Run[]>("/api/runs?status=running"),
      api<AgentRequest[]>("/api/requests"),
    ]);
    setProject(nextProject);
    setTree(nextTree);
    setProposals(nextProposals);
    setRunningRuns(nextRunning);
    setRequests(nextRequests);
  }, []);

  const loadLevel = useCallback(
    async (root: string | null) => {
      try {
        setLevel(await api<Level>(`/api/graph${query({ root })}`));
      } catch (err) {
        // The node you were inside is gone (deleted, perhaps by an agent): go back to the top.
        if (err instanceof ApiError && err.status === 404) setRoute({ root: null, selected: [] });
        else throw err;
      }
    },
    [setRoute],
  );

  const loadDetail = useCallback(async (id: string | null) => {
    if (!id) return setDetail(null);
    try {
      setDetail(await api<NodeDetail>(`/api/nodes/${id}`));
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setDetail(null);
      else throw err;
    }
  }, []);

  const refresh = useCallback(async () => {
    await Promise.all([loadPlan(), loadLevel(route.root), loadDetail(primary)]);
  }, [loadPlan, loadLevel, loadDetail, route.root, primary]);

  useEffect(() => {
    refresh().catch(fail);
  }, [refresh, fail]);

  useLiveEvents((event) => {
    if (event.type === "run.event") return; // the conversation handles these itself
    refresh().catch(fail);
    if (event.type === "proposal.created") {
      const title = byId.get(event.proposal.node_id)?.title ?? "a node";
      toast(`Claude proposed ${event.proposal.data.children.length} children for “${title}”.`);
    }
    if (event.type === "run.request") toast("Claude is waiting for your answer.");
    if (event.type === "run.finished" && event.run.status === "failed" && !event.run.parent_run) {
      toast(`A run failed: ${event.run.error ?? "no reason given"}`);
    }
  });

  // --- navigating ---

  const select = (id: string | null, additive: boolean) => {
    if (id === null) return setRoute({ ...route, selected: [] });
    if (!additive) return setRoute({ ...route, selected: [id] });
    const rest = route.selected.filter((s) => s !== id);
    setRoute({ ...route, selected: rest.length === route.selected.length ? [...rest, id] : rest });
  };
  /** Shows what is inside a node ("" = the top level). */
  const open = (id: string) => setRoute({ root: id || null, selected: id ? [id] : [] });
  /** Shows a node where it lives: its parent's level, with the node selected. */
  const goTo = (id: string) => setRoute({ root: byId.get(id)?.parent ?? null, selected: [id] });
  const showRun = (run: Run) => {
    if (run.node_id) goTo(run.node_id);
    else setRoute({ ...route, selected: [] });
    setFocusRun(run.id);
    if (panes === "details") setPanes("split");
  };

  // --- acting ---

  /** Runs a change, then reloads; errors become a toast. */
  const act = async (change: () => Promise<unknown>) => {
    try {
      await change();
      await refresh();
    } catch (err) {
      fail(err);
      throw err;
    }
  };
  const quietly = (change: () => Promise<unknown>) => void act(change).catch(() => {});
  const patch = (id: string, body: object) => api(`/api/nodes/${id}`, { method: "PATCH", body });

  /** How many nodes sit inside these ones. */
  const insideOf = (ids: string[]): number => {
    const seen = new Set(ids);
    let count = 0;
    const stack = [...ids];
    while (stack.length) {
      const current = stack.pop()!;
      for (const node of nodes) {
        if (node.parent === current && !seen.has(node.id)) {
          seen.add(node.id);
          count += 1;
          stack.push(node.id);
        }
      }
    }
    return count;
  };

  const handlers: ActionHandlers = {
    open,
    add: (parent) => setDialog({ kind: "add", parent }),
    rename: () => {
      if (panes === "chat") setPanes("split");
      window.setTimeout(() => {
        const title = document.getElementById("node-title") as HTMLTextAreaElement | null;
        title?.focus();
        title?.select();
      }, 50);
    },
    setStatus: (ids, status: Status) =>
      quietly(async () => {
        for (const id of ids) await patch(id, { status });
      }),
    toggleKind: (node) => quietly(() => patch(node.id, { kind: node.kind === "work" ? "decision" : "work" })),
    move: (id) => setDialog({ kind: "move", node: id }),
    link: (id) => setDialog({ kind: "link", node: id }),
    merge: (ids) => setDialog({ kind: "merge", nodes: ids }),
    remove: (ids) => setDialog({ kind: "delete", nodes: ids }),
    runAgent: (agent, id) => setDialog({ kind: "run", agent, node: id }),
    check: () =>
      quietly(async () => {
        const fresh = await api<Tree>("/api/tree");
        const count = fresh.errors.length + fresh.warnings.length;
        toast(count ? `${count} ${count === 1 ? "problem" : "problems"} found: see Problems in the details below.` : "The plan checks out: no problems.");
      }),
    reconcile: () =>
      quietly(async () => {
        const result = await api<{ done: string[] }>("/api/reconcile", { method: "POST" });
        toast(result.done.length ? result.done.join(" · ") : "Nothing has merged since last time.");
      }),
    commitEdits: () =>
      quietly(async () => {
        const result = await api<{ sha: string | null; nodes: string[] }>("/api/commit", { method: "POST" });
        toast(result.sha ? `Committed your edits to ${result.nodes.length} node(s).` : "No hand edits to commit.");
      }),
    clear: () => select(null, false),
  };

  const detailActions: DetailActions | null = detail && {
    patch: (change) => act(() => patch(detail.id, change)),
    link: (dependsOn) => act(() => api("/api/edges", { method: "POST", body: { src: detail.id, dst: dependsOn } })),
    unlink: (dependsOn) => act(() => api(`/api/edges${query({ src: detail.id, dst: dependsOn })}`, { method: "DELETE" })),
    goTo,
    review: (proposal) => setDialog({ kind: "review", proposal }),
  };

  if (fatal === "token") {
    return (
      <div className="splash">
        <h1>This page needs its access link</h1>
        <p>
          Open the full address that <code>apm serve</code> printed in your terminal. It ends in <code>?token=…</code>,
          which proves the page was opened by you.
        </p>
      </div>
    );
  }
  if (!project || !tree || !level) {
    return (
      <div className="splash">
        <Spinner /> Loading the plan…
      </div>
    );
  }

  const readyNodes = nodes.filter((n) => n.ready && n.children === 0);
  const issues = [...tree.errors, ...tree.warnings];
  const runOf = (request: AgentRequest) => runningRuns.find((r) => r.id === request.run_id);
  const chatNode = primary ? (byId.get(primary) ?? null) : null;
  const today = new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });

  return (
    <div className="app">
      <header className="masthead">
        <div className="masthead-side">
          <span className="wordmark">APM</span>
          <span className="dateline-text">{today}</span>
        </div>
        <h1 className="nameplate" title={project.code_dir}>
          {project.name}
        </h1>
        <div className="masthead-side right">
          <Search onPick={goTo} />
          {requests.length > 0 && (
            <Menu label={`Needs you ${requests.length}`} title="Claude is waiting for your answer" tone="accent">
              {requests.map((request) => {
                const run = runOf(request);
                return (
                  <button key={request.id} className="menu-item" onClick={() => run && showRun(run)}>
                    {request.kind === "question" ? "A question" : request.kind === "plan" ? "A plan to approve" : "A permission"}
                    {run?.node_id ? ` about “${byId.get(run.node_id)?.title ?? "?"}”` : " in the plan conversation"}
                  </button>
                );
              })}
            </Menu>
          )}
          <Menu label={`Ready ${readyNodes.length}`} title="What can be worked on now">
            {readyNodes.length ? (
              readyNodes.map((n) => (
                <button key={n.id} className="menu-item" onClick={() => goTo(n.id)}>
                  <span className="dot state-ready" />
                  {n.title}
                </button>
              ))
            ) : (
              <p className="empty">Nothing is ready. Everything left is blocked, in progress or done.</p>
            )}
          </Menu>
          {proposals.length > 0 && (
            <Menu label={`Proposals ${proposals.length}`} title="Waiting for your decision" tone="accent">
              {proposals.map((p) => (
                <button key={p.id} className="menu-item" onClick={() => setDialog({ kind: "review", proposal: p })}>
                  {p.data.children.length} children for “{byId.get(p.node_id)?.title ?? "a deleted node"}”
                </button>
              ))}
            </Menu>
          )}
          {runningRuns.length > 0 && (
            <Menu label={`Working ${runningRuns.length}`} title="Agents at work" spinner>
              {runningRuns.map((r) => (
                <button key={r.id} className="menu-item" onClick={() => showRun(r)}>
                  {r.agent.replace("apm-", "")} {r.node_id ? `on “${byId.get(r.node_id)?.title ?? "?"}”` : "on the plan"}
                </button>
              ))}
            </Menu>
          )}
          {issues.length > 0 && (
            <Menu label={`Problems ${issues.length}`} title="Problems in the plan" tone={tree.errors.length ? "bad" : "warn"}>
              {issues.map((issue, i) => (
                <button key={i} className="menu-item" onClick={() => issue.nodes[0] && goTo(issue.nodes[0])}>
                  <strong>{issue.code}</strong> {issue.message}
                </button>
              ))}
            </Menu>
          )}
          <ThemeToggle theme={theme} onChange={setTheme} />
          <span
            className={cx("live", connected ? "on" : "off")}
            title={connected ? "Live: changes appear as they happen" : "Reconnecting…"}
          />
        </div>
      </header>

      <Outline
        nodes={nodes}
        selected={selectedSet}
        root={route.root}
        running={running}
        proposed={proposedOn}
        onPick={(id, additive) => {
          if (additive) select(id, true);
          else goTo(id);
        }}
      />

      <main>
        <Canvas
          level={level}
          selected={selectedSet}
          running={running}
          proposals={proposals}
          onSelect={select}
          onOpen={open}
          onGoTo={goTo}
          onLink={(src, dst) => quietly(() => api("/api/edges", { method: "POST", body: { src, dst } }))}
          onAdd={() => setDialog({ kind: "add", parent: route.root })}
          onDecompose={() => route.root && setDialog({ kind: "run", agent: "apm-decomposer", node: route.root })}
          onReview={(proposal) => setDialog({ kind: "review", proposal })}
        />
      </main>

      <aside className={cx("side", `panes-${panes}`)}>
        <Actions selected={selectedNodes} running={running} handlers={handlers} />
        <div className="pane chat-pane">
          <PaneHead
            title="Conversation"
            sub={chatNode ? `about “${chatNode.title}”` : "about the whole plan"}
            big={panes === "chat"}
            onToggle={() => setPanes(panes === "chat" ? "split" : "chat")}
          />
          <Chat
            context={{ nodeId: chatNode?.id ?? null, title: chatNode?.title ?? project.name }}
            modes={project.modes}
            defaultMode={project.default_mode}
            focus={focusRun}
            onError={toast}
            onChanged={() => void refresh().catch(fail)}
          />
        </div>
        <div className="pane details-pane">
          <PaneHead
            title="Details"
            sub={selectedNodes.length > 1 ? `${selectedNodes.length} nodes` : ""}
            big={panes === "details"}
            onToggle={() => setPanes(panes === "details" ? "split" : "details")}
          />
          <div className="pane-scroll">
            {detail && detailActions && primary ? (
              <NodeDetails detail={detail} nodes={nodes} actions={detailActions} />
            ) : selectedNodes.length > 1 ? (
              <article className="details">
                <div className="kicker">Selection</div>
                <h2 className="headline">{selectedNodes.length} nodes</h2>
                <ul className="refs">
                  {selectedNodes.map((n) => (
                    <li key={n.id}>
                      <span className={cx("dot", `state-${n.status}`)} />
                      <button className="link" onClick={() => goTo(n.id)}>
                        {n.title}
                      </button>
                    </li>
                  ))}
                </ul>
                <p className="lede">Use the actions above to merge them, set their status, or delete them.</p>
              </article>
            ) : (
              <PlanSummary
                project={project}
                nodes={nodes}
                issues={issues}
                proposals={proposals}
                onGoTo={goTo}
                onReview={(proposal) => setDialog({ kind: "review", proposal })}
              />
            )}
          </div>
        </div>
      </aside>

      {dialog?.kind === "add" && (
        <AddNodeDialog
          parent={dialog.parent ? (byId.get(dialog.parent) ?? null) : null}
          onClose={() => setDialog(null)}
          onAdd={async (node) => {
            const created = await api<NodeView>("/api/nodes", { method: "POST", body: { ...node, parent: dialog.parent } });
            setDialog(null);
            setRoute({ root: dialog.parent, selected: [created.id] });
          }}
        />
      )}
      {dialog?.kind === "run" && byId.get(dialog.node) && (
        <RunAgentDialog
          agent={project.agents.find((a) => a.name === dialog.agent)!}
          node={byId.get(dialog.node)!}
          modes={project.modes}
          defaultMode={project.default_mode}
          onClose={() => setDialog(null)}
          onStart={async (message, critic, mode) => {
            const run = await api<Run>("/api/runs", {
              method: "POST",
              body: { agent: dialog.agent, node_id: dialog.node, message, critic, mode },
            });
            setDialog(null);
            setRoute({ ...route, selected: [dialog.node] });
            setFocusRun(run.id);
            if (panes === "details") setPanes("split");
            await refresh();
          }}
        />
      )}
      {dialog?.kind === "move" && byId.get(dialog.node) && (
        <PickNodeDialog
          title={`Move “${byId.get(dialog.node)!.title}”`}
          hint="Everything inside it moves with it."
          placeholder="Put it inside: type a title…"
          nodes={nodes}
          exclude={new Set([dialog.node, byId.get(dialog.node)!.parent ?? ""])}
          extra={byId.get(dialog.node)!.parent ? "Move to the top level" : undefined}
          onClose={() => setDialog(null)}
          onPick={async (parent) => {
            await api(`/api/nodes/${dialog.node}/move`, { method: "POST", body: { parent } });
            setDialog(null);
            setRoute({ root: parent, selected: [dialog.node] });
          }}
        />
      )}
      {dialog?.kind === "link" && byId.get(dialog.node) && (
        <PickNodeDialog
          title={`What does “${byId.get(dialog.node)!.title}” need first?`}
          hint="It will stay blocked until the node you pick is done. You can also drag from one node to another in the graph."
          placeholder="It needs: type a title…"
          nodes={nodes}
          exclude={new Set([dialog.node, ...byId.get(dialog.node)!.depends_on])}
          onClose={() => setDialog(null)}
          onPick={async (dependsOn) => {
            await api("/api/edges", { method: "POST", body: { src: dialog.node, dst: dependsOn } });
            setDialog(null);
            await refresh();
          }}
        />
      )}
      {dialog?.kind === "merge" && (
        <MergeDialog
          nodes={dialog.nodes.map((id) => byId.get(id)).filter((n): n is NodeView => n !== undefined)}
          onClose={() => setDialog(null)}
          onMerge={async (target, others, title) => {
            await api("/api/merge", { method: "POST", body: { target, others, title } });
            setDialog(null);
            setRoute({ ...route, selected: [target] });
          }}
        />
      )}
      {dialog?.kind === "delete" && (
        <DeleteDialog
          nodes={dialog.nodes.map((id) => byId.get(id)).filter((n): n is NodeView => n !== undefined)}
          inside={insideOf(dialog.nodes)}
          onClose={() => setDialog(null)}
          onDelete={async () => {
            const doomed = new Set(dialog.nodes);
            // A node inside another one being deleted goes with it: delete only the outermost.
            const outermost = dialog.nodes.filter((id) => {
              for (let up = byId.get(id)?.parent; up; up = byId.get(up)?.parent) if (doomed.has(up)) return false;
              return true;
            });
            for (const id of outermost) await api(`/api/nodes/${id}${query({ recursive: true })}`, { method: "DELETE" });
            setDialog(null);
            const rootGone = route.root !== null && (doomed.has(route.root) || !byId.has(route.root));
            setRoute({ root: rootGone ? null : route.root, selected: [] });
          }}
        />
      )}
      {dialog?.kind === "review" && (
        <ProposalReview
          proposal={dialog.proposal}
          parentTitle={byId.get(dialog.proposal.node_id)?.title ?? "a deleted node"}
          onClose={() => setDialog(null)}
          onAccept={async (children) => {
            await api(`/api/proposals/${dialog.proposal.id}/accept`, { method: "POST", body: { children } });
            setDialog(null);
            setRoute({ root: dialog.proposal.node_id, selected: [dialog.proposal.node_id] });
          }}
          onReject={async () => {
            await api(`/api/proposals/${dialog.proposal.id}/reject`, { method: "POST" });
            setDialog(null);
            await refresh();
          }}
        />
      )}

      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="toast">
            {t.text}
          </div>
        ))}
      </div>
    </div>
  );
}

function PaneHead({ title, sub, big, onToggle }: { title: string; sub: string; big: boolean; onToggle: () => void }) {
  return (
    <div className="pane-head">
      <span className="label">{title}</span>
      <span className="pane-sub">{sub}</span>
      <button className="icon" onClick={onToggle} title={big ? "Show both panes" : `Give ${title.toLowerCase()} the whole column`}>
        {big ? "Restore" : "Expand"}
      </button>
    </div>
  );
}

const THEMES: { value: Theme; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "Auto" },
];

function ThemeToggle({ theme, onChange }: { theme: Theme; onChange: (theme: Theme) => void }) {
  return (
    <div className="segmented theme" role="group" aria-label="Colour theme">
      {THEMES.map((option) => (
        <button
          key={option.value}
          className={cx(theme === option.value && "on")}
          aria-pressed={theme === option.value}
          title={option.value === "system" ? "Follow your system setting" : `${option.label} theme`}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function Menu({
  label,
  title,
  tone,
  spinner,
  children,
}: {
  label: string;
  title: string;
  tone?: "warn" | "bad" | "accent";
  spinner?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div className="menu" ref={ref}>
      <button className={cx("menu-button", tone)} title={title} aria-expanded={open} onClick={() => setOpen(!open)}>
        {spinner && <Spinner />}
        {label}
      </button>
      {open && (
        <div className="menu-list" onClick={() => setOpen(false)}>
          <div className="menu-title">{title}</div>
          {children}
        </div>
      )}
    </div>
  );
}

function Search({ onPick }: { onPick: (id: string) => void }) {
  const [text, setText] = useState("");
  const [hits, setHits] = useState<{ id: string; handle: string; title: string; snippet: string }[] | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!text.trim()) return setHits(null);
    const timer = window.setTimeout(() => {
      api<typeof hits>(`/api/search${query({ q: text })}`)
        .then(setHits)
        .catch(() => setHits([]));
    }, 150);
    return () => window.clearTimeout(timer);
  }, [text]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes((event.target as HTMLElement).tagName);
      if (event.key === "/" && !typing) {
        event.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="search">
      <input
        ref={input}
        type="search"
        value={text}
        placeholder="Search  /"
        aria-label="Search the plan"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setText("");
            e.currentTarget.blur();
          }
          if (e.key === "Enter" && hits?.[0]) {
            onPick(hits[0].id);
            setText("");
          }
        }}
      />
      {hits && (
        <div className="menu-list">
          {hits.map((hit) => (
            <button
              key={hit.id}
              className="menu-item"
              onClick={() => {
                onPick(hit.id);
                setText("");
              }}
            >
              <span>{hit.title}</span>
              {hit.snippet && <span className="snippet">{hit.snippet}</span>}
            </button>
          ))}
          {!hits.length && <p className="empty">Nothing matches.</p>}
        </div>
      )}
    </div>
  );
}
