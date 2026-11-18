// The whole plan as a collapsible tree, down the left side.

import { useMemo, useState } from "react";
import type { NodeView } from "./types";
import { Spinner } from "./ui";
import { cx, stateOf } from "./util";

export function Outline({
  nodes,
  selected,
  root,
  running,
  proposed,
  onPick,
}: {
  nodes: NodeView[];
  selected: Set<string>;
  root: string | null;
  running: Set<string>;
  proposed: Set<string>;
  onPick: (id: string, additive: boolean) => void;
}) {
  const children = useMemo(() => {
    const map = new Map<string | null, NodeView[]>();
    for (const node of nodes) {
      const list = map.get(node.parent) ?? [];
      list.push(node);
      map.set(node.parent, list);
    }
    return map;
  }, [nodes]);
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setClosed((before) => {
      const after = new Set(before);
      if (!after.delete(id)) after.add(id);
      return after;
    });

  const rows: React.ReactNode[] = [];
  const walk = (parent: string | null, depth: number) => {
    for (const node of children.get(parent) ?? []) {
      const state = stateOf(node);
      const open = !closed.has(node.id);
      rows.push(
        <div
          key={node.id}
          className={cx("outline-row", selected.has(node.id) && "selected", node.id === root && "is-root")}
          style={{ paddingLeft: 8 + depth * 14 }}
          onClick={(e) => onPick(node.id, e.shiftKey || e.metaKey || e.ctrlKey)}
          title={`${node.title} · ${state.label}`}
        >
          {node.children > 0 ? (
            <button
              className="twisty"
              aria-label={open ? "Collapse" : "Expand"}
              onClick={(e) => {
                e.stopPropagation();
                toggle(node.id);
              }}
            >
              {open ? "▾" : "▸"}
            </button>
          ) : (
            <span className="twisty" />
          )}
          <span className={cx("dot", `state-${state.key}`)} />
          <span className={cx("outline-title", node.status === "dropped" && "struck")}>{node.title}</span>
          {running.has(node.id) && <Spinner />}
          {proposed.has(node.id) && <span className="badge proposal">●</span>}
          {node.children > 0 && (
            <span className="outline-count">
              {node.progress.done}/{node.progress.total}
            </span>
          )}
        </div>,
      );
      if (open) walk(node.id, depth + 1);
    }
  };
  walk(null, 0);

  return (
    <nav className="outline" aria-label="Plan outline">
      <div className="outline-head">Plan</div>
      {rows.length ? rows : <p className="empty">No nodes yet.</p>}
    </nav>
  );
}
