// Lays one level out as a left-to-right layered graph with elkjs: what a node needs is on its left.

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

export type Positions = Map<string, { x: number; y: number }>;

export async function layout(boxes: Box[], links: Link[]): Promise<Positions> {
  const known = new Set(boxes.map((b) => b.id));
  const result = await elk.layout({
    id: "level",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "72",
      "elk.spacing.nodeNode": "22",
      "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
      "elk.separateConnectedComponents": "true",
      "elk.spacing.componentComponent": "40",
      // Nodes with no edges stack in a tidy block instead of one long row.
      "elk.aspectRatio": "1.6",
    },
    children: boxes.map((b) => ({ id: b.id, width: b.width, height: b.height })),
    edges: links
      .filter((l) => known.has(l.from) && known.has(l.to))
      .map((l) => ({ id: l.id, sources: [l.from], targets: [l.to] })),
  });
  const positions: Positions = new Map();
  for (const child of result.children ?? []) positions.set(child.id, { x: child.x ?? 0, y: child.y ?? 0 });
  return positions;
}
