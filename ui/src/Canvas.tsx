// One level of the plan as a graph: the children of the current node, the edges between them
// (lifted from wherever they are stored), and stubs for edges that leave the level.

import {
  Background,
  BackgroundVariant,
  type Connection,
  Controls,
  type Edge,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { type Box, type Link, type Positions, layout } from "./layout";
import type { Level, LevelPort, NodeView, Proposal, ProposedChild } from "./types";
import { Spinner, StatePill } from "./ui";
import { cx, stateOf } from "./util";

const CARD = { width: 240, height: 100 };
const CHIP = { width: 176, height: 30 };

type PlanData = { node: NodeView; selected: boolean; running: boolean; proposed: boolean; entangled: boolean };
type GhostData = { child: ProposedChild };
type PortData = { port: LevelPort };

const PlanCard = memo(function PlanCard({ data }: NodeProps<Node<PlanData>>) {
  const { node, selected, running, proposed, entangled } = data;
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
        {data.child.title}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
});

const PortChip = memo(function PortChip({ data }: NodeProps<Node<PortData>>) {
  const { port } = data;
  const label = port.direction === "out" ? "needs" : "needed by";
  return (
    <div className="port" title={`${label} ${port.outside.title} (outside this level). Click to go there.`}>
      {port.direction === "in" && <Handle type="target" position={Position.Left} isConnectable={false} />}
      <span className="port-label">{port.direction === "out" ? "from" : "to"}</span>
      <span className="port-title">{port.outside.title}</span>
      {port.direction === "out" && <Handle type="source" position={Position.Right} isConnectable={false} />}
    </div>
  );
});

const NODE_TYPES = { plan: PlanCard, ghost: GhostCard, port: PortChip };

export interface CanvasProps {
  level: Level;
  selected: string | null;
  running: Set<string>;
  proposals: Proposal[];
  /** The side panel is open, so the canvas is narrower. */
  narrow: boolean;
  onSelect: (id: string | null) => void;
  onOpen: (id: string) => void;
  onGoTo: (id: string) => void;
  onLink: (src: string, dst: string) => void;
  onAdd: () => void;
  onDecompose: () => void;
  onReview: (proposal: Proposal) => void;
}

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
  narrow,
  onSelect,
  onOpen,
  onGoTo,
  onLink,
  onAdd,
  onDecompose,
  onReview,
}: CanvasProps) {
  const flow = useReactFlow();
  const rootId = level.root?.id ?? null;
  const proposal = proposals.find((p) => p.node_id === rootId) ?? null;
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
  const [positions, setPositions] = useState<{ signature: string; at: Positions }>({ signature: "", at: new Map() });
  const fitted = useRef("");
  useEffect(() => {
    let cancelled = false;
    void layout(boxes, links).then((at) => {
      if (!cancelled) setPositions({ signature, at });
    });
    return () => {
      cancelled = true;
    };
    // Lay out again only when the shape of the level changes, not on every status change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  // Fit the view when you arrive at a level, and when the side panel opens or closes (that changes
  // the room there is). A level that merely updates keeps your pan and zoom. The viewport is worked
  // out from the layout itself, so it never depends on what React Flow has measured so far.
  const frame = useRef<HTMLDivElement>(null);
  const topInset = 56 + (proposal ? 64 : 0) + (level.entangled.length ? 64 : 0);
  const fitKey = `${rootId ?? "top"}:${narrow}:${signature}`;
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

  const nodes = useMemo<Node[]>(() => {
    const out: Node[] = [];
    const place = (id: string) => positions.at.get(id);
    for (const node of level.nodes) {
      const position = place(node.id);
      if (!position) continue;
      const data: PlanData = {
        node,
        selected: node.id === selected,
        running: running.has(node.id),
        proposed: proposedOn.has(node.id),
        entangled: entangled.has(node.id),
      };
      out.push({ id: node.id, type: "plan", position, data, ...CARD, draggable: false });
    }
    for (const [id, port] of ports) {
      const position = place(id);
      if (position) {
        out.push({ id, type: "port", position, data: { port }, ...CHIP, draggable: false, connectable: false });
      }
    }
    for (const child of proposal?.data.children ?? []) {
      const id = `ghost:${child.key}`;
      const position = place(id);
      if (position) {
        out.push({ id, type: "ghost", position, data: { child }, ...CARD, draggable: false, connectable: false });
      }
    }
    return out;
  }, [level.nodes, ports, proposal, positions, selected, running, proposedOn, entangled]);

  const edges = useMemo<Edge[]>(() => {
    const marker = { type: MarkerType.ArrowClosed, width: 16, height: 16 };
    const out: Edge[] = level.edges.map((e) => ({
      id: `e:${e.dst}:${e.src}`,
      source: e.dst,
      target: e.src,
      markerEnd: marker,
      label: e.count > 1 ? String(e.count) : undefined,
      className: cx("dep", entangled.has(e.src) && entangled.has(e.dst) && "entangled"),
      selectable: false,
    }));
    for (const link of links) {
      if (link.id.startsWith("p:") || link.id.startsWith("g:")) {
        out.push({
          id: link.id,
          source: link.from,
          target: link.to,
          markerEnd: marker,
          className: link.id.startsWith("p:") ? "dep port-edge" : "dep ghost-edge",
          selectable: false,
        });
      }
    }
    return out;
  }, [level.edges, links, entangled]);

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
        <button className="primary" onClick={onAdd}>
          + Add {level.root ? "child" : "node"}
        </button>
        {level.root && (
          <button onClick={onDecompose} title="Ask Claude to break this node into children">
            Decompose with Claude
          </button>
        )}
        {running.has(rootId ?? "") && (
          <span className="note">
            <Spinner /> Claude is working on this node…
          </span>
        )}
      </div>
      {proposal && (
        <div className="banner proposal">
          <span>
            <strong>Claude proposed {proposal.data.children.length} children.</strong> They are shown dashed and are
            not part of the plan until you accept them.
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
      {level.nodes.length === 0 && !proposal ? (
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
          nodesDraggable={false}
          nodesConnectable
          elementsSelectable={false}
          zoomOnDoubleClick={false}
          minZoom={0.2}
          maxZoom={1.5}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, node) => {
            if (node.type === "plan") onSelect(node.id);
            else if (node.type === "port") onGoTo((node.data as PortData).port.outside.id);
            else if (node.type === "ghost" && proposal) onReview(proposal);
          }}
          onNodeDoubleClick={(_, node) => node.type === "plan" && onOpen(node.id)}
          onPaneClick={() => onSelect(null)}
          onConnect={connect}
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
          <Controls showInteractive={false} position="bottom-left" />
        </ReactFlow>
      )}
    </div>
  );
}
