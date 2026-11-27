// Lays one level out as a left-to-right layered graph with elkjs: what a node needs is on its left.
// ELK also routes the edges, orthogonally, between the nodes rather than under them: an edge that
// skips layers goes round the nodes in between, and its route is drawn as it is.

import ELK from "elkjs/lib/elk.bundled.js";

const elk = new ELK();

export interface Box {
  id: string;
  width: number;
  height: number;
}

export interface Link {
  id: string;
  from: string;
  to: string;
}

export interface Point {
  x: number;
  y: number;
}

export type Positions = Map<string, Point>;

export interface Layout {
  at: Positions;
  /** Each link's route, from the edge of its source to the edge of its target. */
  routes: Map<string, Point[]>;
}

interface ElkSection {
  startPoint: Point;
  endPoint: Point;
  bendPoints?: Point[];
}

export async function layout(boxes: Box[], links: Link[]): Promise<Layout> {
  const known = new Set(boxes.map((b) => b.id));
  const result = await elk.layout({
    id: "level",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.edgeRouting": "ORTHOGONAL",
      // Room between layers for the routes, and between a route and the nodes it passes.
      "elk.layered.spacing.nodeNodeBetweenLayers": "96",
      "elk.layered.spacing.edgeNodeBetweenLayers": "28",
      "elk.layered.spacing.edgeEdgeBetweenLayers": "10",
      "elk.spacing.nodeNode": "28",
      "elk.spacing.edgeNode": "20",
      "elk.spacing.edgeEdge": "10",
      "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
      "elk.layered.crossingMinimization.semiInteractive": "false",
      "elk.separateConnectedComponents": "true",
      "elk.spacing.componentComponent": "48",
      // Nodes with no edges stack in a tidy block instead of one long row.
      "elk.aspectRatio": "1.6",
    },
    children: boxes.map((b) => ({ id: b.id, width: b.width, height: b.height })),
    edges: links
      .filter((l) => known.has(l.from) && known.has(l.to))
      .map((l) => ({ id: l.id, sources: [l.from], targets: [l.to] })),
  });
  const at: Positions = new Map();
  for (const child of result.children ?? []) at.set(child.id, { x: child.x ?? 0, y: child.y ?? 0 });
  const routes = new Map<string, Point[]>();
  for (const edge of result.edges ?? []) {
    const sections = (edge as { sections?: ElkSection[] }).sections ?? [];
    const points = sections.flatMap((s) => [s.startPoint, ...(s.bendPoints ?? []), s.endPoint]);
    if (points.length >= 2) routes.set(edge.id, points);
  }
  return { at, routes };
}

/** A route as an SVG path: straight runs joined by rounded corners. */
export function roundedPath(points: Point[], radius = 10): string {
  const [first, ...rest] = points;
  if (!first) return "";
  let d = `M${first.x},${first.y}`;
  for (let i = 0; i < rest.length; i++) {
    const here = rest[i]!;
    const next = rest[i + 1];
    const prev = i === 0 ? first : rest[i - 1]!;
    if (!next) {
      d += ` L${here.x},${here.y}`;
      break;
    }
    // Shorten both legs at this corner by the radius (or half a short leg), and curve between.
    const r = Math.min(radius, dist(prev, here) / 2, dist(here, next) / 2);
    const a = toward(here, prev, r);
    const b = toward(here, next, r);
    d += ` L${a.x},${a.y} Q${here.x},${here.y} ${b.x},${b.y}`;
  }
  return d;
}

const dist = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

function toward(from: Point, to: Point, length: number): Point {
  const d = dist(from, to) || 1;
  return { x: from.x + ((to.x - from.x) * length) / d, y: from.y + ((to.y - from.y) * length) / d };
}

/** The point halfway along a route, for a label. */
export function midpoint(points: Point[]): Point {
  const total = points.slice(1).reduce((sum, p, i) => sum + dist(points[i]!, p), 0);
  let left = total / 2;
  for (let i = 1; i < points.length; i++) {
    const leg = dist(points[i - 1]!, points[i]!);
    if (left <= leg) return toward(points[i - 1]!, points[i]!, left);
    left -= leg;
  }
  return points.at(-1) ?? { x: 0, y: 0 };
}
