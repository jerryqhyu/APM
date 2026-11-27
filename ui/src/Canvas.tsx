// One level of the plan as a graph: the children of the current node, the edges between them
// (lifted from wherever they are stored), and stubs for edges that leave the level.

import {
  Background,
  BackgroundVariant,
  BaseEdge,
  type Connection,
  Controls,
  type Edge,
  type EdgeProps,
  getBezierPath,
  Handle,
  MarkerType,
  type Node,
  type NodeHandle,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { type Box, type Layout, type Link, type Point, layout, midpoint, roundedPath } from "./layout";
import type { Nav } from "./layout-chrome";
import type { Level, LevelPort, NodeView, Proposal, ProposedChild } from "./types";
import { NodeList } from "./NodeList";
import { Spinner, StatePill } from "./ui";
import { cx, isSplit, proposalSummary, stateOf } from "./util";

const CARD = { width: 240, height: 100 };
const CHIP = { width: 176, height: 30 };

/** Where a node's edges attach: the middle of its left side (in) and of its right side (out).
 * Given with every node, so React Flow draws edges without measuring the cards first. It drops
 * what it measured whenever it is handed new node objects (a selection, a refresh, stepping back
 * out of a feature), and until it measured again it drew no edges at all.
 */
function sides({ width, height }: { width: number; height: number }, into = true, out = true): NodeHandle[] {
  const middle = height / 2 - 0.5;
  return [
    ...(into ? [{ type: "target" as const, position: Position.Left, x: 0, y: middle, width: 1, height: 1 }] : []),
    ...(out ? [{ type: "source" as const, position: Position.Right, x: width - 1, y: middle, width: 1, height: 1 }] : []),
  ];
}
const CARD_SIDES = sides(CARD);

/** How a node relates to the one you selected: it, what it needs (up), what needs it (down), or
 * neither (dim). Null when nothing is selected.
 */
type Relation = "focus" | "up" | "down" | "dim" | null;
type PlanData = {
  node: NodeView;
  selected: boolean;
  running: boolean;
  proposed: boolean;
  entangled: boolean;
  relation: Relation;
};
type GhostData = { child: ProposedChild };
type PortData = { port: LevelPort; relation: Relation };
type RouteData = { points?: Point[] };

const PlanCard = memo(function PlanCard({ data }: NodeProps<Node<PlanData>>) {
  const { node, selected, running, proposed, entangled, relation } = data;
  const state = stateOf(node);
  const percent = node.progress.total ? (100 * node.progress.done) / node.progress.total : 0;
  return (
    <div
      className={cx(
        "card",
        `state-${state.key}`,
        selected && "selected",
        entangled && "entangled",
        node.kind === "decision" && "decision",
        node.kind === "review" && "review-step",
        relation && `rel-${relation}`,
      )}
      title={node.children ? "Double-click to open" : undefined}
    >
      <Handle type="target" position={Position.Left} />
      <div className="card-top">
        <StatePill node={node} />
        <span className="card-badges">
          {running && <Spinner />}
          {proposed && <span className="badge proposal">proposal</span>}
          {node.dropped_deps.length > 0 && (
            <span className="badge warn" title="Depends on a dropped node">
              !
            </span>
          )}
          <span className="handle">{node.handle}</span>
        </span>
      </div>
      <div className="card-title">
        {node.kind === "decision" && (
          <span className="kind" title="Decision">
            ◆{" "}
          </span>
        )}
        {node.kind === "review" && (
          <span className="kind review" title="Review step: checks the feature once every task is done">
            ✓{" "}
          </span>
        )}
        {node.title}
      </div>
      {node.children > 0 && (
        <div className="card-progress">
          <div className="bar">
            <span style={{ width: `${percent}%` }} />
          </div>
          <span>
            {node.progress.done}/{node.progress.total}
          </span>
          <span className="open-hint">open ›</span>
        </div>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
});

const GhostCard = memo(function GhostCard({ data }: NodeProps<Node<GhostData>>) {
  return (
    <div className="card ghost">
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="card-top">
        <span className="pill state-proposed">
          <span className="dot" />
          Proposed
        </span>
      </div>
      <div className="card-title">
        {data.child.kind === "decision" && <span className="kind">◆ </span>}
        {data.child.kind === "review" && <span className="kind review">✓ </span>}
        {data.child.title}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
});

const PortChip = memo(function PortChip({ data }: NodeProps<Node<PortData>>) {
  const { port, relation } = data;
  const label = port.direction === "out" ? "needs" : "needed by";
  return (
    <div className={cx("port", relation && `rel-${relation}`)} title={`${label} ${port.outside.title} (outside this level). Click to go there.`}>
      {port.direction === "in" && <Handle type="target" position={Position.Left} isConnectable={false} />}
      <span className="port-label">{port.direction === "out" ? "from" : "to"}</span>
      <span className="port-title">{port.outside.title}</span>
      {port.direction === "out" && <Handle type="source" position={Position.Right} isConnectable={false} />}
    </div>
  );
});

const NODE_TYPES = { plan: PlanCard, ghost: GhostCard, port: PortChip };

/** An edge drawn along the route the layout found for it, round the nodes in between, rather than
 * a curve straight through them. Before the layout knows a route, a plain curve.
 */
const RoutedEdge = memo(function RoutedEdge(props: EdgeProps<Edge<RouteData>>) {
  const { id, data, markerEnd, style, label } = props;
  const [curve, curveX, curveY] = getBezierPath(props);
  const points = data?.points;
  const path = points ? roundedPath(points) : curve;
  const mid = points ? midpoint(points) : { x: curveX, y: curveY };
  return <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} label={label} labelX={mid.x} labelY={mid.y} />;
});

const EDGE_TYPES = { routed: RoutedEdge };

/** Everything `start` leads to by following `next`, transitively, without `start` itself. */
function reach(start: string, next: Map<string, string[]>): Set<string> {
  const seen = new Set<string>();
  const todo = [start];
  while (todo.length) {
    for (const other of next.get(todo.pop()!) ?? []) {
      if (other !== start && !seen.has(other)) {
        seen.add(other);
        todo.push(other);
      }
    }
  }
  return seen;
}

export interface CanvasProps {
  level: Level;
  /** Every selected node. Shift-click or ⌘/ctrl-click adds to the selection. */
  selected: Set<string>;
  running: Set<string>;
  proposals: Proposal[];
  onSelect: (id: string | null, additive: boolean) => void;
  onOpen: (id: string) => void;
  onGoTo: (id: string) => void;
  onLink: (src: string, dst: string) => void;
  onAdd: () => void;
  onDecompose: () => void;
  onReview: (proposal: Proposal) => void;
  /** Graph or list: the same level either way. */
  view: View;
  onView: (view: View) => void;
  /** Whether Back and Forward (through the levels you visited) have somewhere to go. */
  nav: Nav;
}

export type View = "graph" | "list";

export function Canvas(props: CanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function CanvasInner({
  level,
  selected,
  running,
  proposals,
  onSelect,
  onOpen,
  onGoTo,
  onLink,
  onAdd,
  onDecompose,
  onReview,
  view,
  onView,
  nav,
}: CanvasProps) {
  const flow = useReactFlow();
  const rootId = level.root?.id ?? null;
  // A breakdown shows inside its node; a split shows next to the task it would replace.
  const proposal =
    proposals.find((p) => !isSplit(p) && p.node_id === rootId) ??
    proposals.find((p) => isSplit(p) && level.nodes.some((n) => n.id === p.node_id)) ??
    null;
  const proposedOn = useMemo(() => new Set(proposals.map((p) => p.node_id)), [proposals]);

  // What to lay out: plan nodes, port chips, and the ghost children of a pending proposal.
  const { boxes, links, ports } = useMemo(() => {
    const boxes: Box[] = level.nodes.map((n) => ({ id: n.id, ...CARD }));
    const links: Link[] = level.edges.map((e) => ({ id: `e:${e.dst}:${e.src}`, from: e.dst, to: e.src }));
    const ports = new Map<string, LevelPort>();
    for (const port of level.ports) {
      const id = `port:${port.direction}:${port.outside.id}`;
      if (!ports.has(id)) {
        ports.set(id, port);
        boxes.push({ id, ...CHIP });
      }
      links.push(
        port.direction === "out"
          ? { id: `p:${id}:${port.node}`, from: id, to: port.node }
          : { id: `p:${port.node}:${id}`, from: port.node, to: id },
      );
    }
    for (const child of proposal?.data.children ?? []) {
      const id = `ghost:${child.key}`;
      boxes.push({ id, ...CARD });
      for (const key of child.depends_on_keys) links.push({ id: `g:${key}:${child.key}`, from: `ghost:${key}`, to: id });
      for (const ref of child.depends_on_ids) {
        const target = level.nodes.find((n) => n.id === ref || n.id.replaceAll("-", "").endsWith(ref.toLowerCase()));
        if (target) links.push({ id: `g:${target.id}:${child.key}`, from: target.id, to: id });
      }
    }
    return { boxes, links, ports };
  }, [level, proposal]);

  const signature = useMemo(
    () => JSON.stringify([boxes.map((b) => b.id), links.map((l) => l.id)]),
    [boxes, links],
  );
  const [positions, setPositions] = useState<{ signature: string } & Layout>({
    signature: "",
    at: new Map(),
    routes: new Map(),
  });
  const fitted = useRef("");
  useEffect(() => {
    let cancelled = false;
    void layout(boxes, links).then((result) => {
      if (!cancelled) setPositions({ signature, ...result });
    });
    return () => {
      cancelled = true;
    };
    // Lay out again only when the shape of the level changes, not on every status change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  // Fit the view when you arrive at a level, or when its shape changes. A level that merely updates
  // keeps your pan and zoom. The viewport is worked out from the layout itself, so it never depends on what React Flow has measured so far.
  const frame = useRef<HTMLDivElement>(null);
  const topInset = 56 + (proposal ? 64 : 0) + (level.entangled.length ? 64 : 0);
  const fitKey = `${rootId ?? "top"}:${signature}`;
  useEffect(() => {
    if (positions.signature !== signature || fitted.current === fitKey || boxes.length === 0) return;
    const timer = window.setTimeout(() => {
      const size = frame.current?.getBoundingClientRect();
      if (!size || size.width < 50) return;
      fitted.current = fitKey;
      let [minX, minY, maxX, maxY] = [Infinity, Infinity, -Infinity, -Infinity];
      for (const box of boxes) {
        const at = positions.at.get(box.id);
        if (!at) continue;
        minX = Math.min(minX, at.x);
        minY = Math.min(minY, at.y);
        maxX = Math.max(maxX, at.x + box.width);
        maxY = Math.max(maxY, at.y + box.height);
      }
      // Routes can run round the outside of the nodes: keep them in view too.
      for (const route of positions.routes.values()) {
        for (const point of route) {
          minX = Math.min(minX, point.x);
          minY = Math.min(minY, point.y);
          maxX = Math.max(maxX, point.x);
          maxY = Math.max(maxY, point.y);
        }
      }
      const margin = 36;
      const room = { width: size.width - 2 * margin, height: size.height - topInset - 2 * margin };
      const zoom = Math.max(0.3, Math.min(1, room.width / (maxX - minX), room.height / (maxY - minY)));
      void flow.setViewport(
        {
          zoom,
          x: margin + (room.width - (maxX - minX) * zoom) / 2 - minX * zoom,
          y: topInset + margin + Math.max(0, (room.height - (maxY - minY) * zoom) / 2) * 0.6 - minY * zoom,
        },
        { duration: 180 },
      );
    }, 80); // let the canvas take its new size first
    return () => window.clearTimeout(timer);
  }, [positions, signature, fitKey, boxes, topInset, flow]);

  const entangled = useMemo(() => new Set(level.entangled.flat()), [level.entangled]);

  // Select one node and its chain stands out: what it needs, all the way back (up), and what needs
  // it, all the way on (down). The rest dims.
  const focus = selected.size === 1 ? [...selected][0]! : null;
  const { up, down } = useMemo(() => {
    if (!focus || !level.nodes.some((n) => n.id === focus)) return { up: new Set<string>(), down: new Set<string>() };
    const needs = new Map<string, string[]>();
    const neededBy = new Map<string, string[]>();
    for (const e of level.edges) {
      needs.set(e.src, [...(needs.get(e.src) ?? []), e.dst]);
      neededBy.set(e.dst, [...(neededBy.get(e.dst) ?? []), e.src]);
    }
    return { up: reach(focus, needs), down: reach(focus, neededBy) };
  }, [focus, level.nodes, level.edges]);
  const lit = focus !== null && level.nodes.some((n) => n.id === focus);
  const relationOf = (id: string): Relation =>
    !lit ? null : id === focus ? "focus" : up.has(id) ? "up" : down.has(id) ? "down" : "dim";
  // A stub for an edge out of the level is in the chain when its node is, and its direction runs
  // the same way: what an upstream node needs is upstream too.
  const portRelation = (port: LevelPort): Relation => {
    const node = relationOf(port.node);
    if (node === null || node === "dim") return node;
    const way = port.direction === "out" ? "up" : "down";
    return node === "focus" || node === way ? way : "dim";
  };

  const nodes = useMemo<Node[]>(() => {
    const out: Node[] = [];
    const place = (id: string) => positions.at.get(id);
    for (const node of level.nodes) {
      const position = place(node.id);
      if (!position) continue;
      const data: PlanData = {
        node,
        selected: selected.has(node.id),
        running: running.has(node.id),
        proposed: proposedOn.has(node.id),
        entangled: entangled.has(node.id),
        relation: relationOf(node.id),
      };
      out.push({ id: node.id, type: "plan", position, data, ...CARD, handles: CARD_SIDES, draggable: false });
    }
    for (const [id, port] of ports) {
      const position = place(id);
      if (position) {
        const handles = sides(CHIP, port.direction === "in", port.direction === "out");
        const data: PortData = { port, relation: portRelation(port) };
        out.push({ id, type: "port", position, data, ...CHIP, handles, draggable: false, connectable: false });
      }
    }
    for (const child of proposal?.data.children ?? []) {
      const id = `ghost:${child.key}`;
      const position = place(id);
      if (position) {
        out.push({ id, type: "ghost", position, data: { child }, ...CARD, handles: CARD_SIDES, draggable: false, connectable: false });
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level.nodes, ports, proposal, positions, selected, running, proposedOn, entangled, up, down, lit]);

  const edges = useMemo<Edge[]>(() => {
    const arrow = { type: MarkerType.ArrowClosed, width: 16, height: 16 };
    const route = (id: string): RouteData => ({ points: positions.routes.get(id) });
    // An edge runs from what is needed (dst) to what needs it (src).
    const lane = (needed: string, needing: string): "up" | "down" | "dim" | null => {
      if (!lit) return null;
      if ((needing === focus || up.has(needing)) && up.has(needed)) return "up";
      if ((needed === focus || down.has(needed)) && down.has(needing)) return "down";
      return "dim";
    };
    const out: Edge[] = level.edges.map((e) => {
      const id = `e:${e.dst}:${e.src}`;
      const relation = lane(e.dst, e.src);
      return {
        id,
        type: "routed",
        source: e.dst,
        target: e.src,
        data: route(id),
        markerEnd: arrow,
        label: e.count > 1 ? String(e.count) : undefined,
        className: cx("dep", entangled.has(e.src) && entangled.has(e.dst) && "entangled", relation),
        zIndex: relation === "up" || relation === "down" ? 1 : 0,
        selectable: false,
      };
    });
    for (const link of links) {
      if (link.id.startsWith("p:") || link.id.startsWith("g:")) {
        const port = ports.get(link.from) ?? ports.get(link.to);
        const shown = port ? portRelation(port) : lit ? "dim" : null;
        out.push({
          id: link.id,
          type: "routed",
          source: link.from,
          target: link.to,
          data: route(link.id),
          markerEnd: arrow,
          className: cx("dep", link.id.startsWith("p:") ? "port-edge" : "ghost-edge", shown),
          selectable: false,
        });
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level.edges, links, entangled, positions, up, down, lit, focus, ports]);

  const connect = (connection: Connection) => {
    // Dragging from A to B says "B needs A": B depends on A.
    if (connection.source && connection.target && connection.source !== connection.target) {
      onLink(connection.target, connection.source);
    }
  };

  const ready = positions.signature === signature;
  return (
    <div className="canvas" ref={frame}>
      <div className="canvas-bar">
        <div className="nav-buttons" role="group" aria-label="History">
          <button className="icon" aria-label="Back" title="Back to the level you were on" disabled={!nav.back} onClick={() => window.history.back()}>
            ‹
          </button>
          <button className="icon" aria-label="Forward" title="Forward again" disabled={!nav.forward} onClick={() => window.history.forward()}>
            ›
          </button>
        </div>
        <nav className="crumbs" aria-label="Where you are">
          <button className={cx("crumb", !level.root && "here")} onClick={() => onOpen("")}>
            All
          </button>
          {level.breadcrumb.map((crumb, i) => (
            <span key={crumb.id}>
              <span className="sep">/</span>
              <button className={cx("crumb", i === level.breadcrumb.length - 1 && "here")} onClick={() => onOpen(crumb.id)}>
                {crumb.title}
              </button>
            </span>
          ))}
        </nav>
        <span className="spacer" />
        <div className="segmented" role="group" aria-label="Show as">
          {(["graph", "list"] as const).map((v) => (
            <button key={v} className={cx(view === v && "on")} aria-pressed={view === v} onClick={() => onView(v)}>
              {v === "graph" ? "Graph" : "List"}
            </button>
          ))}
        </div>
        {running.has(rootId ?? "") && (
          <span className="note">
            <Spinner /> Claude is working on this node…
          </span>
        )}
      </div>
      {proposal && (
        <div className="banner proposal">
          <span>
            <strong>
              {isSplit(proposal)
                ? proposalSummary(proposal, level.nodes.find((n) => n.id === proposal.node_id)?.title ?? "a task")
                : `Claude proposed ${proposal.data.children.length} children.`}
            </strong>{" "}
            They are shown dashed and are not part of the plan until you accept them
            {isSplit(proposal) ? ": then they replace the task, here." : "."}
          </span>
          <button className="primary" onClick={() => onReview(proposal)}>
            Review proposal
          </button>
        </div>
      )}
      {level.entangled.length > 0 && (
        <div className="banner warn">
          <span>
            <strong>Entangled:</strong> the highlighted nodes each contain work the other is waiting for. The breakdown
            may need reshaping.
          </span>
        </div>
      )}
      {view === "list" ? (
        <NodeList
          nodes={level.nodes}
          top={!level.root}
          selected={selected}
          running={running}
          onSelect={(id, additive) => onSelect(id, additive)}
          onOpen={onOpen}
        />
      ) : level.nodes.length === 0 && !proposal ? (
        <div className="canvas-empty">
          <h2>{level.root ? "Nothing inside this node yet" : "Your plan is empty"}</h2>
          <p>
            {level.root
              ? "Add children by hand, or let Claude propose a breakdown for you to review."
              : "Start with the big idea. You can break it down afterwards."}
          </p>
          <div className="row">
            <button className="primary" onClick={onAdd}>
              + Add {level.root ? "child" : "your first node"}
            </button>
            {level.root && <button onClick={onDecompose}>Decompose with Claude</button>}
          </div>
        </div>
      ) : (
        <ReactFlow
          nodes={ready ? nodes : []}
          edges={ready ? edges : []}
          nodeTypes={NODE_TYPES}
          edgeTypes={EDGE_TYPES}
          nodesDraggable={false}
          nodesConnectable
          elementsSelectable={false}
          zoomOnDoubleClick={false}
          minZoom={0.2}
          maxZoom={1.5}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(event, node) => {
            if (node.type === "plan") onSelect(node.id, event.shiftKey || event.metaKey || event.ctrlKey);
            else if (node.type === "port") onGoTo((node.data as PortData).port.outside.id);
            else if (node.type === "ghost" && proposal) onReview(proposal);
          }}
          onNodeDoubleClick={(_, node) => node.type === "plan" && onOpen(node.id)}
          onPaneClick={() => onSelect(null, false)}
          onConnect={connect}
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
          {lit && (up.size > 0 || down.size > 0) && (
            <div className="relation-legend" aria-live="polite">
              <span>Needs first: {up.size}</span>
              <span>Waits on it: {down.size}</span>
            </div>
          )}
          <Controls showInteractive={false} position="bottom-left" />
        </ReactFlow>
      )}
    </div>
  );
}
