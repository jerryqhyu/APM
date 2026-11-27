import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import mark from "./assets/apm-mark.svg";
import { type ActionHandlers, ActionsMenu } from "./Actions";
import {
  ApiError,
  api,
  clearNodeSettings,
  download,
  implementAll,
  query,
  redoEager,
  reviewAllTasks,
  saveNodeSettings,
  writeReport,
} from "./api";
import { Canvas, type View } from "./Canvas";
import { Chat } from "./Chat";
import { type DetailActions, NodeDetails, PlanSummary } from "./Details";
import { AddNodeDialog, DeleteDialog, ExportDialog, MergeDialog, PickNodeDialog, RunAgentDialog } from "./Dialogs";
import { Grip, type Nav, SidebarIcon, navState, navigate, setHashParam, startNav, useStoredNumber } from "./layout-chrome";
import { useLiveConnection, useLiveEvents } from "./live";
import { Outline } from "./Outline";
import { ProposalReview } from "./ProposalReview";
import { SettingsDialog } from "./Settings";
import { type Theme, useTheme } from "./theme";
import type { AgentRequest, Level, NodeDetail, NodeView, Project, Proposal, Run, Status, Tree } from "./types";
import { Spinner } from "./ui";
import { AGENT_LABEL, type Thread, cx, errorText, isSplit, proposalSummary, stackedName, threadsOf, timeAgo } from "./util";

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
  | { kind: "review"; proposal: Proposal }
  | { kind: "export" }
  | { kind: "settings" };

type Tab = "chat" | "details";

function hashParams(): URLSearchParams {
  return new URLSearchParams(window.location.hash.replace(/^#/, ""));
}

function readRoute(): Route {
  const params = hashParams();
  const node = params.get("node");
  return { root: params.get("in"), selected: node ? [node] : [] };
}

const routeHash = (route: Route) => query({ in: route.root, node: route.selected.at(-1) }).replace(/^\?/, "#");

/** Puts `route` in the address. Going to another level is a step Back can undo; picking another
 * node on the same level is not. The conversation in view (`chat`) is added by the chat itself.
 */
function writeRoute(route: Route): void {
  const before = readRoute();
  const hash = routeHash(route);
  if (hash !== routeHash(before)) navigate(hash, route.root !== before.root);
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
  const [route, setRouteState] = useState<Route>(() => {
    startNav();
    return readRoute();
  });
  const [nav, setNav] = useState<Nav>(navState);
  const [outline, setOutline] = useRemembered<"shown" | "hidden">("apm-outline", "shown", ["shown", "hidden"]);
  const [outlineWidth, setOutlineWidth] = useStoredNumber("apm-outline-width", null);
  const [sideWidth, setSideWidth] = useStoredNumber("apm-side-width", null);
  const sideRef = useRef<HTMLElement>(null);
  const outlineRef = useRef<HTMLDivElement>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  // Details first: the readable account of a node. Chat is one click away.
  const [tab, setTab] = useState<Tab>(() => (hashParams().get("chat") ? "chat" : "details"));
  const [view, setView] = useRemembered<View>("apm-view", "graph", ["graph", "list"]);
  // The conversation to show: one you just started, or the one the address names (`#…&chat=`).
  const [focusRun, setFocusRun] = useState<string | null>(() => hashParams().get("chat"));
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
  // What the right panel is about: the node you picked, else the feature you are inside.
  const focusId = primary ?? (route.selected.length === 0 ? route.root : null);

  const toast = useCallback((text: string) => {
    const id = ++toastId.current;
    setToasts((before) => [...before, { id, text }]);
    window.setTimeout(() => setToasts((before) => before.filter((t) => t.id !== id)), 7000);
  }, []);

  const setRoute = useCallback((next: Route) => {
    writeRoute(next);
    setRouteState(next);
    setNav(navState());
  }, []);

  useEffect(() => {
    const onChange = () => {
      setRouteState(readRoute());
      setNav(navState());
    };
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

  // Reloading is coalesced: a change usually asks twice (after the request, and again when the
  // live "plan changed" event arrives). While one reload runs, any number of further requests
  // become a single reload after it.
  const reload = useCallback(async () => {
    await Promise.all([loadPlan(), loadLevel(route.root), loadDetail(focusId)]);
  }, [loadPlan, loadLevel, loadDetail, route.root, focusId]);
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  const inFlight = useRef<Promise<void> | null>(null);
  const queued = useRef<Promise<void> | null>(null);
  const refresh = useCallback((): Promise<void> => {
    if (!inFlight.current) {
      inFlight.current = reloadRef.current().finally(() => {
        inFlight.current = null;
      });
      return inFlight.current;
    }
    if (!queued.current) {
      queued.current = inFlight.current
        .catch(() => {})
        .then(() => {
          queued.current = null;
          return refresh();
        });
    }
    return queued.current;
    // reloadRef always holds the latest reload, so this function never needs to change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    reload().catch(fail);
  }, [reload, fail]);

  useLiveEvents((event) => {
    if (event.type === "run.event") return; // the conversation handles these itself
    refresh().catch(fail);
    if (event.type === "proposal.created") {
      toast(proposalSummary(event.proposal, byId.get(event.proposal.node_id)?.title ?? "a node"));
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
    setTab("chat");
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
      setTab("details");
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
        const result = await api<{ done: string[]; findings: string[] }>("/api/reconcile", { method: "POST" });
        const lines = [...result.done, ...result.findings];
        toast(lines.length ? lines.join(" · ") : "Nothing has reached the default branch since last time.");
      }),
    commitEdits: () =>
      quietly(async () => {
        const result = await api<{ sha: string | null; nodes: string[] }>("/api/commit", { method: "POST" });
        toast(result.sha ? `Committed your edits to ${result.nodes.length} node(s).` : "No hand edits to commit.");
      }),
    exportArchive: () => setDialog({ kind: "export" }),
    openSettings: () => setDialog({ kind: "settings" }),
    reviewTasks: async (id) => {
      const batch = await reviewAllTasks(id);
      await refresh();
      return batch;
    },
    implementAll: async (id) => {
      const batch = await implementAll(id);
      await refresh();
      return batch;
    },
    clear: () => select(null, false),
  };

  const detailActions: DetailActions | null = detail && {
    patch: (change) => act(() => patch(detail.id, change)),
    link: (dependsOn) => act(() => api("/api/edges", { method: "POST", body: { src: detail.id, dst: dependsOn } })),
    unlink: (dependsOn) => act(() => api(`/api/edges${query({ src: detail.id, dst: dependsOn })}`, { method: "DELETE" })),
    goTo,
    review: (proposal) => setDialog({ kind: "review", proposal }),
    // Errors show next to the settings, so these don't toast.
    saveSettings: async (change) => {
      await saveNodeSettings(detail.id, change);
      await refresh();
    },
    clearSettings: async () => {
      await clearNodeSettings(detail.id);
      await refresh();
    },
    // Errors show next to the Redo button.
    eagerRedo: async () => {
      showRun(await redoEager(detail.id));
      await refresh();
    },
    runAgent: (agent, id) => setDialog({ kind: "run", agent, node: id }),
    implementAll: handlers.implementAll,
    openRun: (runId) => {
      setFocusRun(runId);
      setTab("chat");
    },
  };

  if (fatal === "token") {
    return (
      <div className="splash">
        <img className="splash-mark" src={mark} alt="APM" width={72} height={72} />
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
        <img className="splash-mark" src={mark} alt="APM" width={72} height={72} />
        <span>
          <Spinner /> Loading the plan…
        </span>
      </div>
    );
  }

  const readyNodes = nodes.filter((n) => n.ready && n.children === 0);
  const issues = [...tree.errors, ...tree.warnings];
  const runOf = (request: AgentRequest) => runningRuns.find((r) => r.id === request.run_id);
  const chatNode = focusId ? (byId.get(focusId) ?? null) : null;

  return (
    <div
      className={cx("app", outline === "hidden" && "outline-hidden")}
      style={
        {
          ...(outlineWidth ? { "--outline-w": `${outlineWidth}px` } : {}),
          ...(sideWidth ? { "--side-w": `${sideWidth}px` } : {}),
        } as React.CSSProperties
      }
    >
      <header className="topbar">
        <button
          className="icon sidebar-toggle"
          aria-pressed={outline === "shown"}
          aria-label={outline === "shown" ? "Hide the outline" : "Show the outline"}
          title={outline === "shown" ? "Hide the outline" : "Show the outline"}
          onClick={() => setOutline(outline === "shown" ? "hidden" : "shown")}
        >
          <SidebarIcon open={outline === "shown"} />
        </button>
        <div className="brand" title={project.code_dir}>
          <img className="logo" src={mark} alt="" width={22} height={22} />
          <span className="project-name">{project.name}</span>
        </div>
        <Search onPick={goTo} />
        <div className="topbar-right">
          <ConversationsMenu byId={byId} onOpen={showRun} onError={toast} />
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
                  <span className={cx("dot", n.stack_top ? "state-stacked" : "state-ready")} />
                  {n.title}
                  {n.stack_top && <span className="menu-note">stacked on {stackedName(n)}</span>}
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

      <div className="outline-panel" ref={outlineRef}>
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
      </div>
      <Grip
        className="grip-outline"
        label="Outline width"
        onResize={(dx) => {
          const now = outlineWidth ?? outlineRef.current?.getBoundingClientRect().width ?? 240;
          setOutlineWidth(Math.min(480, Math.max(160, now + dx)));
        }}
        onReset={() => setOutlineWidth(null)}
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
          view={view}
          onView={setView}
          nav={nav}
        />
      </main>
      <Grip
        className="grip-side"
        label="Conversation width"
        onResize={(dx) => {
          const now = sideWidth ?? sideRef.current?.getBoundingClientRect().width ?? 480;
          setSideWidth(Math.min(window.innerWidth * 0.7, Math.max(320, now - dx)));
        }}
        onReset={() => setSideWidth(null)}
      />

      <aside className="side" ref={sideRef}>
        <div className="side-head">
          <div className="tabs" role="tablist">
            <button
              role="tab"
              aria-selected={tab === "details"}
              className={cx("tab", tab === "details" && "active")}
              onClick={() => setTab("details")}
            >
              Details
            </button>
            <button role="tab" aria-selected={tab === "chat"} className={cx("tab", tab === "chat" && "active")} onClick={() => setTab("chat")}>
              Chat
              <span className="tab-sub">{chatNode ? chatNode.title : "whole plan"}</span>
            </button>
          </div>
          <ActionsMenu
            selected={selectedNodes}
            running={running}
            handlers={handlers}
            lookup={(id) => byId.get(id)}
            detail={detail}
            nodes={nodes}
            here={route.root ? (byId.get(route.root) ?? null) : null}
          />
        </div>
        <div className={cx("tab-panel", tab !== "chat" && "hidden")}>
          <Chat
            context={{ nodeId: chatNode?.id ?? null, title: chatNode?.title ?? project.name }}
            modes={project.modes}
            defaultMode={project.default_mode}
            focus={focusRun}
            onError={toast}
            onChanged={() => void refresh().catch(fail)}
            onShown={(threadId) => setHashParam("chat", threadId)}
            titleOf={(id) => byId.get(id)?.title}
          />
        </div>
        <div className={cx("tab-panel", "scroll", tab !== "details" && "hidden")}>
          {detail && detailActions && focusId ? (
            <NodeDetails detail={detail} nodes={nodes} actions={detailActions} />
          ) : selectedNodes.length > 1 ? (
            <article className="details">
              <h2 className="headline">{selectedNodes.length} nodes selected</h2>
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
              <p className="hint">Use Actions above to merge them, set their status, or delete them.</p>
            </article>
          ) : (
            <PlanSummary
              project={project}
              nodes={nodes}
              issues={issues}
              proposals={proposals}
              requests={requests}
              runs={runningRuns}
              onGoTo={goTo}
              onReview={(proposal) => setDialog({ kind: "review", proposal })}
              onOpenRun={(runId) => {
                const run = runningRuns.find((r) => r.id === runId);
                if (run) showRun(run);
              }}
              onImplement={(id) => setDialog({ kind: "run", agent: "apm-implementer", node: id })}
            />
          )}
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
          onStart={async (message, critic, mode, branch) => {
            const run =
              dialog.agent === "apm-feature-reporter"
                ? await writeReport(dialog.node, { message, mode })
                : await api<Run>("/api/runs", {
                    method: "POST",
                    body: { agent: dialog.agent, node_id: dialog.node, message, critic, mode, branch: branch || null },
                  });
            setDialog(null);
            // Implement on a task of an eager feature builds the feature: follow that run there.
            if (run.node_id && run.node_id !== dialog.node) showRun(run);
            else {
              setRoute({ ...route, selected: [dialog.node] });
              setFocusRun(run.id);
              setTab("chat");
            }
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
            const { proposal } = dialog;
            const done = await api<{ created: string[] }>(`/api/proposals/${proposal.id}/accept`, {
              method: "POST",
              body: { children },
            });
            setDialog(null);
            // A split replaces the task where it was; a breakdown fills the node: go and see them.
            if (isSplit(proposal) && byId.get(proposal.node_id)?.parent) {
              setRoute({ root: byId.get(proposal.node_id)!.parent, selected: done.created.slice(0, 1) });
            } else setRoute({ root: proposal.node_id, selected: [proposal.node_id] });
          }}
          onReject={async () => {
            await api(`/api/proposals/${dialog.proposal.id}/reject`, { method: "POST" });
            setDialog(null);
            await refresh();
          }}
        />
      )}
      {dialog?.kind === "settings" && (
        <SettingsDialog
          nodes={nodes}
          onClose={() => setDialog(null)}
          onGoTo={goTo}
          onSaved={(said) => {
            toast(said);
            refresh().catch(fail);
          }}
        />
      )}
      {dialog?.kind === "export" && (
        <ExportDialog
          projectName={project.name}
          onClose={() => setDialog(null)}
          onExport={async (withRuns) => {
            const name = await download(`/api/export${query({ with_runs: withRuns })}`, `${project.name}.apm.zip`);
            setDialog(null);
            toast(`Saved ${name}.`);
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

/** Every conversation in the plan, newest first: chats and every agent's runs, whatever they are
 * about. Pick one to read it, or reply to it, in the Chat panel.
 */
function ConversationsMenu({
  byId,
  onOpen,
  onError,
}: {
  byId: Map<string, NodeView>;
  onOpen: (run: Run) => void;
  onError: (text: string) => void;
}) {
  const [threads, setThreads] = useState<Thread[] | null>(null);
  const load = () =>
    void api<Run[]>("/api/runs")
      .then((runs) => setThreads(threadsOf(runs).slice(0, 40)))
      .catch((err) => onError(errorText(err)));
  return (
    <Menu label="Conversations" title="Every conversation in the plan, newest first" onOpen={load}>
      {threads === null ? (
        <p className="empty">Loading…</p>
      ) : threads.length === 0 ? (
        <p className="empty">No conversations yet.</p>
      ) : (
        threads.map((thread) => {
          const about = thread.runs[0]!.node_id;
          return (
            <button key={thread.id} className="menu-item" onClick={() => onOpen(thread.runs.at(-1)!)}>
              {thread.running && <Spinner />}
              <span>{thread.title}</span>
              <span className="menu-note">
                {about ? `“${byId.get(about)?.title ?? "a deleted node"}”` : "the whole plan"} ·{" "}
                {AGENT_LABEL[thread.agent] ?? thread.agent} · {timeAgo(thread.updated)}
              </span>
            </button>
          );
        })
      )}
    </Menu>
  );
}

function Menu({
  label,
  title,
  tone,
  spinner,
  onOpen,
  children,
}: {
  label: string;
  title: string;
  tone?: "warn" | "bad" | "accent";
  spinner?: boolean;
  /** Called as the list opens: to load what it shows. */
  onOpen?: () => void;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    // Capture: the graph stops mousedown from bubbling up to the window.
    window.addEventListener("mousedown", close, true);
    return () => window.removeEventListener("mousedown", close, true);
  }, [open]);
  return (
    <div className="menu" ref={ref}>
      <button
        className={cx("menu-button", tone)}
        title={title}
        aria-expanded={open}
        onClick={() => {
          if (!open) onOpen?.();
          setOpen(!open);
        }}
      >
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

/** A choice this browser remembers (in localStorage, when it may). */
function useRemembered<T extends string>(key: string, fallback: T, allowed: readonly T[]): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(key) as T | null;
      return stored && allowed.includes(stored) ? stored : fallback;
    } catch {
      return fallback;
    }
  });
  const remember = useCallback(
    (next: T) => {
      setValue(next);
      try {
        localStorage.setItem(key, next);
      } catch {
        // private browsing: the choice lasts for this page only
      }
    },
    [key],
  );
  return [value, remember];
}
