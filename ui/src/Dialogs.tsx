// The small dialogs: add a node, start an agent, move a node, delete a node.

import { useState } from "react";
import { NodePicker } from "./NodePanel";
import type { Agent, Kind, NodeView } from "./types";
import { Modal } from "./ui";
import { AGENT_LABEL } from "./util";

function useSubmit(action: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };
  return { busy, error, submit };
}

export function AddNodeDialog({
  parent,
  onAdd,
  onClose,
}: {
  parent: NodeView | null;
  onAdd: (node: { title: string; kind: Kind; intent: string }) => Promise<void>;
  onClose: () => void;
}) {
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<Kind>("work");
  const [intent, setIntent] = useState("");
  const { busy, error, submit } = useSubmit(() => onAdd({ title: title.trim(), kind, intent }));
  return (
    <Modal
      title={parent ? `Add a child to “${parent.title}”` : "Add a node"}
      onClose={onClose}
      footer={
        <>
          {error && <span className="form-error">{error}</span>}
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy || !title.trim()} onClick={() => void submit()}>
            Add
          </button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (title.trim()) void submit();
        }}
      >
        <label className="field">
          <span>Title</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="An outcome, in a few words" />
        </label>
        <label className="field">
          <span>Kind</span>
          <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            <option value="work">Work: something to build</option>
            <option value="decision">Decision: a choice to make</option>
          </select>
        </label>
        <label className="field">
          <span>Why it exists (optional)</span>
          <textarea rows={3} value={intent} onChange={(e) => setIntent(e.target.value)} />
        </label>
      </form>
    </Modal>
  );
}

const GUIDANCE: Record<string, { label: string; placeholder: string; go: string }> = {
  "apm-decomposer": {
    label: "Guidance (optional)",
    placeholder: "For example: keep it to three steps, or slice by user-visible feature.",
    go: "Decompose",
  },
  "apm-implementer": {
    label: "Guidance (optional)",
    placeholder: "Anything Claude should know beyond what the node says.",
    go: "Start building",
  },
  "apm-assistant": {
    label: "Your question",
    placeholder: "For example: is this well specified? What would you do first?",
    go: "Ask",
  },
};

export function RunAgentDialog({
  agent,
  node,
  onStart,
  onClose,
}: {
  agent: Agent;
  node: NodeView;
  onStart: (guidance: string, critic: boolean) => Promise<void>;
  onClose: () => void;
}) {
  const [guidance, setGuidance] = useState("");
  const [critic, setCritic] = useState(true);
  const { busy, error, submit } = useSubmit(() => onStart(guidance, critic));
  const text = GUIDANCE[agent.name] ?? GUIDANCE["apm-assistant"]!;
  return (
    <Modal
      title={`${AGENT_LABEL[agent.name] ?? agent.name}: “${node.title}”`}
      onClose={onClose}
      footer={
        <>
          {error && <span className="form-error">{error}</span>}
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy} onClick={() => void submit()}>
            {text.go}
          </button>
        </>
      }
    >
      <p className="hint">{agent.summary}</p>
      <label className="field">
        <span>{text.label}</span>
        <textarea
          rows={4}
          value={guidance}
          placeholder={text.placeholder}
          onChange={(e) => setGuidance(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void submit();
          }}
        />
      </label>
      {agent.name === "apm-decomposer" && (
        <label className="check">
          <input type="checkbox" checked={critic} onChange={(e) => setCritic(e.target.checked)} />
          Have a second agent review the breakdown first (slower, costs a little more)
        </label>
      )}
    </Modal>
  );
}

export function MoveDialog({
  node,
  nodes,
  onMove,
  onClose,
}: {
  node: NodeView;
  nodes: NodeView[];
  onMove: (parent: string | null) => Promise<void>;
  onClose: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const move = (parent: string | null) => {
    setError(null);
    onMove(parent).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  };
  return (
    <Modal title={`Move “${node.title}”`} onClose={onClose} footer={error && <span className="form-error">{error}</span>}>
      <p className="hint">Everything inside it moves with it.</p>
      <NodePicker
        nodes={nodes}
        exclude={new Set([node.id, node.parent ?? ""])}
        placeholder="New parent: type a title…"
        onPick={move}
        autoFocus
      />
      {node.parent && (
        <p>
          <button onClick={() => move(null)}>Move to the top level</button>
        </p>
      )}
    </Modal>
  );
}

export function DeleteDialog({
  node,
  descendants,
  onDelete,
  onClose,
}: {
  node: NodeView;
  descendants: number;
  onDelete: () => Promise<void>;
  onClose: () => void;
}) {
  const { busy, error, submit } = useSubmit(onDelete);
  return (
    <Modal
      title={`Delete “${node.title}”?`}
      onClose={onClose}
      footer={
        <>
          {error && <span className="form-error">{error}</span>}
          <button onClick={onClose}>Cancel</button>
          <button className="danger solid" disabled={busy} onClick={() => void submit()}>
            {descendants ? `Delete it and ${descendants} inside it` : "Delete"}
          </button>
        </>
      }
    >
      <p>
        {descendants
          ? `This also deletes the ${descendants} ${descendants === 1 ? "node" : "nodes"} inside it. `
          : ""}
        Dependencies on what you delete are removed. The plan's history keeps it: nothing is lost for good.
      </p>
    </Modal>
  );
}
