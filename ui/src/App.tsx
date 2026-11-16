import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, query } from "./api";
import { Canvas } from "./Canvas";
import { AddNodeDialog, DeleteDialog, MoveDialog, RunAgentDialog } from "./Dialogs";
import { useLiveConnection, useLiveEvents } from "./live";
import { NodePanel, type PanelActions } from "./NodePanel";
import { Outline } from "./Outline";
import { ProposalReview } from "./ProposalReview";
import type { Level, NodeDetail, NodeView, Project, Proposal, Run, Tree } from "./types";
import { Spinner } from "./ui";
import { cx } from "./util";

interface Route {
  root: string | null;
  selected: string | null;
}

type Dialog =
  | { kind: "add"; parent: string | null }
  | { kind: "run"; agent: string; node: string }
  | { kind: "move"; node: string }
  | { kind: "delete"; node: string }
  | { kind: "review"; proposal: Proposal };

function readRoute(): Route {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  return { root: params.get("in"), selected: params.get("node") };
}

function writeRoute(route: Route): void {
  const hash = query({ in: route.root, node: route.selected }).replace(/^\?/, "#");
  if (hash !== window.location.hash) window.history.pushState(null, "", hash || window.location.pathname);
}

export function App() {
  const connected = useLiveConnection();
  const [project, setProject] = useState<Project | null>(null);
  const [tree, setTree] = useState<Tree | null>(null);
  const [level, setLevel] = useState<Level | null>(null);
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [runningRuns, setRunningRuns] = useState<Run[]>([]);
  const [route, setRouteState] = useState<Route>(readRoute);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [tab, setTab] = useState<"details" | "runs" | "history">("details");
  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const [fatal, setFatal] = useState<string | null>(null);
  const toastId = useRef(0);

  const nodes = tree?.nodes ?? [];
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const running = useMemo(() => new Set(runningRuns.map((r) => r.node_id)), [runningRuns]);
  const proposedOn = useMemo(() => new Set(proposals.map((p) => p.node_id)), [proposals]);

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
      else toast(err instanceof Error ? err.message : String(err));
    },
    [toast],
  );

  const loadPlan = useCallback(async () => {
    const [nextProject, nextTree, nextProposals, nextRunning] = await Promise.all([
      api<Project>("/api/project"),
      api<Tree>("/api/tree"),
      api<Proposal[]>("/api/proposals"),
      api<Run[]>("/api/runs?status=running"),
    ]);
    setProject(nextProject);
    setTree(nextTree);
    setProposals(nextProposals);
    setRunningRuns(nextRunning);
  }, []);

  const loadLevel = useCallback(async (root: string | null) => {
    try {
      setLevel(await api<Level>(`/api/graph${query({ root })}`));
    } catch (err) {
      // The node you were inside is gone (deleted, perhaps by an agent): go back to the top.
      if (err instanceof ApiError && err.status === 404) setRoute({ root: null, selected: null });
      else throw err;
    }
  }, [setRoute]);

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
    await Promise.all([loadPlan(), loadLevel(route.root), loadDetail(route.selected)]);
  }, [loadPlan, loadLevel, loadDetail, route.root, route.selected]);

  useEffect(() => {
    refresh().catch(fail);
  }, [refresh, fail]);

  useLiveEvents((event) => {
    if (event.type === "run.event") return; // the transcript handles these itself
    refresh().catch(fail);
    if (event.type === "proposal.created") {
      const title = byId.get(event.proposal.node_id)?.title ?? "a node";
      toast(`Claude proposed ${event.proposal.data.children.length} children for “${title}”.`);
    }
    if (event.type === "run.finished" && event.run.status === "failed" && !event.run.parent_run) {
      toast(`A run failed: ${event.run.error ?? "no reason given"}`);
    }
  });

  // --- navigating ---

  const select = (id: string | null) => {
    setRoute({ ...route, selected: id });
    if (id !== route.selected) setTab("details");
  };
  const open = (id: string) => setRoute({ root: id, selected: id });
  /** Shows a node where it lives: its parent's level, with the node selected. */
  const goTo = (id: string) => {
    setRoute({ root: byId.get(id)?.parent ?? null, selected: id });
    setTab("details");
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

  const actions: PanelActions | null = detail && {
    patch: (change) => act(() => api(`/api/nodes/${detail.id}`, { method: "PATCH", body: change })),
    link: (dependsOn) => act(() => api("/api/edges", { method: "POST", body: { src: detail.id, dst: dependsOn } })),
    unlink: (dependsOn) => act(() => api(`/api/edges${query({ src: detail.id, dst: dependsOn })}`, { method: "DELETE" })),
    open: () => open(detail.id),
    goTo,
    addChild: () => setDialog({ kind: "add", parent: detail.id }),
    runAgent: (agent) => setDialog({ kind: "run", agent, node: detail.id }),
    move: () => setDialog({ kind: "move", node: detail.id }),
    remove: () => setDialog({ kind: "delete", node: detail.id }),
    review: (proposal) => setDialog({ kind: "review", proposal }),
    stopRun: (run) => act(() => api(`/api/runs/${run.id}`, { method: "DELETE" })),
    continueRun: async (run, message) => {
      await api(`/api/runs/${run.id}/continue`, { method: "POST", body: { message } });
      await refresh();
    },
    close: () => select(null),
  };

  const descendantsOf = (id: string): number => {
    let count = 0;
    const stack = [id];
    while (stack.length) {
      const current = stack.pop()!;
      for (const node of nodes) {
        if (node.parent === current) {
          count += 1;
          stack.push(node.id);
        }
      }
    }
    return count;
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

  return (
    <div className={cx("app", detail && "with-panel")}>
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>
            ◈
          </span>
          {project.name}
        </div>
        <nav className="crumbs" aria-label="Where you are">
          <button className={cx("crumb", !level.root && "here")} onClick={() => setRoute({ root: null, selected: null })}>
            Top
          </button>
          {level.breadcrumb.map((crumb, i) => (
            <span key={crumb.id}>
              <span className="sep">›</span>
              <button className={cx("crumb", i === level.breadcrumb.length - 1 && "here")} onClick={() => open(crumb.id)}>
                {crumb.title}
              </button>
            </span>
          ))}
        </nav>
        <span className="spacer" />
        <Search onPick={goTo} />
        <Menu label={`Ready ${readyNodes.length}`} title="What can be worked on now" tone={readyNodes.length ? "good" : undefined}>
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
          <Menu label={`Running ${runningRuns.length}`} title="Agents at work" spinner>
            {runningRuns.map((r) => (
              <button
                key={r.id}
                className="menu-item"
                onClick={() => {
                  goTo(r.node_id);
                  setTab("runs");
                }}
              >
                {r.agent.replace("apm-", "")} on “{byId.get(r.node_id)?.title ?? "?"}”
              </button>
            ))}
          </Menu>
        )}
        {issues.length > 0 && (
          <Menu label={`⚠ ${issues.length}`} title="Problems in the plan" tone={tree.errors.length ? "bad" : "warn"}>
            {issues.map((issue, i) => (
              <button key={i} className="menu-item" onClick={() => issue.nodes[0] && goTo(issue.nodes[0])}>
                <strong>{issue.code}</strong> {issue.message}
              </button>
            ))}
          </Menu>
        )}
        <span className={cx("live", connected ? "on" : "off")} title={connected ? "Live: changes appear as they happen" : "Reconnecting…"} />
      </header>

      <Outline nodes={nodes} selected={route.selected} root={route.root} running={running} proposed={proposedOn} onPick={goTo} />

      <main>
        <Canvas
          level={level}
          selected={route.selected}
          running={running}
          proposals={proposals}
          narrow={detail !== null}
          onSelect={select}
          onOpen={open}
          onGoTo={goTo}
          onLink={(src, dst) => void act(() => api("/api/edges", { method: "POST", body: { src, dst } })).catch(() => {})}
          onAdd={() => setDialog({ kind: "add", parent: route.root })}
          onDecompose={() => route.root && setDialog({ kind: "run", agent: "apm-decomposer", node: route.root })}
          onReview={(proposal) => setDialog({ kind: "review", proposal })}
        />
      </main>

      {detail && actions && <NodePanel detail={detail} nodes={nodes} actions={actions} tab={tab} onTab={setTab} />}

      {dialog?.kind === "add" && (
        <AddNodeDialog
          parent={dialog.parent ? (byId.get(dialog.parent) ?? null) : null}
          onClose={() => setDialog(null)}
          onAdd={async (node) => {
            const created = await api<NodeView>("/api/nodes", { method: "POST", body: { ...node, parent: dialog.parent } });
            setDialog(null);
            setRoute({ root: dialog.parent, selected: created.id });
          }}
        />
      )}
      {dialog?.kind === "run" && byId.get(dialog.node) && (
        <RunAgentDialog
          agent={project.agents.find((a) => a.name === dialog.agent) ?? { name: dialog.agent, summary: "" }}
          node={byId.get(dialog.node)!}
          onClose={() => setDialog(null)}
          onStart={async (guidance, critic) => {
            await api("/api/runs", { method: "POST", body: { agent: dialog.agent, node_id: dialog.node, guidance, critic } });
            setDialog(null);
            setRoute({ ...route, selected: dialog.node });
            setTab("runs");
            await refresh();
          }}
        />
      )}
      {dialog?.kind === "move" && byId.get(dialog.node) && (
        <MoveDialog
          node={byId.get(dialog.node)!}
          nodes={nodes}
          onClose={() => setDialog(null)}
          onMove={async (parent) => {
            await api(`/api/nodes/${dialog.node}/move`, { method: "POST", body: { parent } });
            setDialog(null);
            setRoute({ root: parent, selected: dialog.node });
          }}
        />
      )}
      {dialog?.kind === "delete" && byId.get(dialog.node) && (
        <DeleteDialog
          node={byId.get(dialog.node)!}
          descendants={descendantsOf(dialog.node)}
          onClose={() => setDialog(null)}
          onDelete={async () => {
            const parent = byId.get(dialog.node)?.parent ?? null;
            await api(`/api/nodes/${dialog.node}${query({ recursive: true })}`, { method: "DELETE" });
            setDialog(null);
            setRoute({ root: route.root === dialog.node ? parent : route.root, selected: null });
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
            setRoute({ root: dialog.proposal.node_id, selected: dialog.proposal.node_id });
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

function Menu({
  label,
  title,
  tone,
  spinner,
  children,
}: {
  label: string;
  title: string;
  tone?: "good" | "warn" | "bad" | "accent";
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
        placeholder="Search the plan  /"
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
