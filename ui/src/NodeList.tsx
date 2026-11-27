// The list view: the nodes of the level you are on (features at the top, a feature's tasks inside
// it), searchable, filterable by tag, and on the top level by whether a feature is broken down yet.

import { useMemo, useState } from "react";
import type { NodeView } from "./types";
import { StatePill } from "./ui";
import { cx } from "./util";

/** A feature with nothing inside it yet: it waits for its breakdown. */
export const notBrokenDown = (node: NodeView) => node.kind === "work" && node.children === 0;

export function TagChips({ tags, active, onPick }: { tags: string[]; active?: Set<string>; onPick?: (tag: string) => void }) {
  if (!tags.length) return null;
  return (
    <span className="tags">
      {tags.map((tag) =>
        onPick ? (
          <button
            key={tag}
            className={cx("tag", active?.has(tag) && "on")}
            onClick={(event) => {
              event.stopPropagation();
              onPick(tag);
            }}
          >
            #{tag}
          </button>
        ) : (
          <span key={tag} className="tag">
            #{tag}
          </span>
        ),
      )}
    </span>
  );
}

/** Words match a node's title, handle or tags; `#tag` and `tag:tag` must be one of its tags. */
function matches(node: NodeView, words: string[], tags: Set<string>): boolean {
  if ([...tags].some((tag) => !node.tags.includes(tag))) return false;
  const text = `${node.title} ${node.handle} ${node.tags.join(" ")}`.toLowerCase();
  return words.every((word) => text.includes(word));
}

export function NodeList({
  nodes,
  top,
  selected,
  running,
  onSelect,
  onOpen,
}: {
  nodes: NodeView[];
  /** The top level, where the nodes are features and some may not be broken down yet. */
  top: boolean;
  selected: Set<string>;
  running: Set<string>;
  onSelect: (id: string, additive: boolean) => void;
  onOpen: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [onlyBacklog, setOnlyBacklog] = useState(false);

  const counts = useMemo(() => {
    const count = new Map<string, number>();
    for (const node of nodes) for (const tag of node.tags) count.set(tag, (count.get(tag) ?? 0) + 1);
    return [...count].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [nodes]);

  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const tags = new Set([
    ...picked,
    ...words.filter((w) => w.startsWith("#") || w.startsWith("tag:")).map((w) => w.replace(/^(#|tag:)/, "")),
  ]);
  tags.delete("");
  const plain = words.filter((w) => !w.startsWith("#") && !w.startsWith("tag:"));
  const backlog = top ? nodes.filter(notBrokenDown).length : 0;
  const shown = nodes.filter((n) => matches(n, plain, tags) && (!onlyBacklog || notBrokenDown(n)));
  const toggle = (tag: string) =>
    setPicked((now) => {
      const next = new Set(now);
      if (!next.delete(tag)) next.add(tag);
      return next;
    });

  return (
    <div className="node-list">
      <div className="list-tools">
        <input
          type="search"
          placeholder={`Search ${top ? "features" : "this level"}: words, #tag`}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search the list"
        />
        {top && backlog > 0 && (
          <label className="check">
            <input type="checkbox" checked={onlyBacklog} onChange={(event) => setOnlyBacklog(event.target.checked)} />
            Not broken down ({backlog})
          </label>
        )}
      </div>
      {counts.length > 0 && (
        <div className="list-tags" aria-label="Filter by tag">
          {counts.map(([tag, count]) => (
            <button key={tag} className={cx("tag", picked.has(tag) && "on")} onClick={() => toggle(tag)}>
              #{tag} <span className="count">{count}</span>
            </button>
          ))}
          {picked.size > 0 && (
            <button className="link" onClick={() => setPicked(new Set())}>
              Clear
            </button>
          )}
        </div>
      )}
      <table>
        <thead>
          <tr>
            <th>{top ? "Feature" : "Node"}</th>
            <th>State</th>
            <th>Progress</th>
            <th>Tags</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((node) => (
            <tr
              key={node.id}
              className={cx(selected.has(node.id) && "selected")}
              onClick={(event) => onSelect(node.id, event.shiftKey || event.metaKey || event.ctrlKey)}
              onDoubleClick={() => node.children > 0 && onOpen(node.id)}
            >
              <td>
                <span className="list-title">{node.title}</span> <code className="handle">{node.handle}</code>
                {running.has(node.id) && <span className="note"> · Claude is working</span>}
              </td>
              <td>
                <StatePill node={node} />
              </td>
              <td>
                {node.children > 0 ? (
                  <button className="link" onClick={() => onOpen(node.id)} title="Show what is inside">
                    {node.progress.done}/{node.progress.total} done
                  </button>
                ) : top && notBrokenDown(node) ? (
                  <span className="badge">Not broken down</span>
                ) : node.kind === "review" ? (
                  <span className="hint">Review step</span>
                ) : (
                  <span className="hint">{node.kind === "decision" ? "Decision" : "Task"}</span>
                )}
              </td>
              <td>
                <TagChips tags={node.tags} active={tags} onPick={toggle} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown.length === 0 && (
        <p className="hint list-empty">
          {nodes.length ? "Nothing here matches." : top ? "No features yet: add one." : "Nothing inside this node yet."}
        </p>
      )}
    </div>
  );
}

/** Tags of one node, with a field to add more. Changes save at once. */
export function TagEditor({ tags, onSave }: { tags: string[]; onSave: (tags: string[]) => Promise<void> }) {
  const [text, setText] = useState("");
  const add = () => {
    const fresh = text.split(/[,\s]+/).map((t) => t.replace(/^#/, "").trim().toLowerCase()).filter(Boolean);
    setText("");
    if (fresh.some((t) => !tags.includes(t))) void onSave([...new Set([...tags, ...fresh])]);
  };
  return (
    <div className="tag-editor">
      {tags.map((tag) => (
        <span key={tag} className="tag">
          #{tag}
          <button className="icon" aria-label={`Remove #${tag}`} onClick={() => void onSave(tags.filter((t) => t !== tag))}>
            ×
          </button>
        </span>
      ))}
      <input
        value={text}
        placeholder={tags.length ? "Add a tag" : "Add tags, e.g. backlog"}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === ",") {
            event.preventDefault();
            add();
          }
        }}
        onBlur={() => text.trim() && add()}
        aria-label="Add a tag"
      />
    </div>
  );
}
