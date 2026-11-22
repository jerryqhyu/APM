// The small dialogs: add a node, start an agent, move, link, merge, delete and export.

import { useState } from "react";
import { NodePicker } from "./Details";
import type { Agent, Kind, Mode, NodeView } from "./types";
import { Modal } from "./ui";
import { errorText } from "./util";

function useSubmit(action: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(errorText(err));
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
      title={parent ? `Add a node inside “${parent.title}”` : "Add a node"}
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

const GUIDANCE: Record<string, { placeholder: string; go: string }> = {
  "apm-decomposer": { placeholder: "For example: keep it to three steps, or slice by user-visible feature.", go: "Decompose" },
  "apm-implementer": { placeholder: "Anything Claude should know beyond what the node says.", go: "Start building" },
  "apm-feature-reviewer": { placeholder: "For example: look hard at error handling.", go: "Start review" },
  "apm-feature-reporter": {
    placeholder: "For example: say more about the storage format. The decisions and their reasons come from the plan's record.",
    go: "Write report",
  },
};

export function RunAgentDialog({
  agent,
  node,
  modes,
  defaultMode,
  onStart,
  onClose,
}: {
  agent: Agent;
  node: NodeView;
  modes: Mode[];
  defaultMode: string;
  onStart: (message: string, critic: boolean, mode: string, branch: string) => Promise<void>;
  onClose: () => void;
}) {
  const [message, setMessage] = useState("");
  const [critic, setCritic] = useState(true);
  const [mode, setMode] = useState(defaultMode);
  const [branch, setBranch] = useState("");
  const { busy, error, submit } = useSubmit(() => onStart(message, critic, mode, branch.trim()));
  const text = GUIDANCE[agent.name] ?? { placeholder: "", go: "Start" };
  return (
    <Modal
      title={`${agent.label}: “${node.title}”`}
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
        <span>Guidance (optional)</span>
        <textarea
          rows={4}
          value={message}
          placeholder={text.placeholder}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void submit();
          }}
        />
      </label>
      <label className="field">
        <span>Permissions</span>
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          {modes.map((m) => (
            <option key={m.name} value={m.name}>
              {m.label}
            </option>
          ))}
        </select>
      </label>
      {agent.name === "apm-feature-reviewer" && (
        <label className="field">
          <span>Branch (optional)</span>
          <input
            value={branch}
            placeholder={node.delivery?.branch ?? node.feature_branch ?? "APM works it out from the tasks"}
            onChange={(e) => setBranch(e.target.value)}
          />
        </label>
      )}
      {agent.name === "apm-decomposer" && (
        <label className="check">
          <input type="checkbox" checked={critic} onChange={(e) => setCritic(e.target.checked)} />
          Have a second agent review the breakdown first (slower, costs a little more)
        </label>
      )}
    </Modal>
  );
}

/** Picks one other node: the new parent for a move, or the dependency for a link. */
export function PickNodeDialog({
  title,
  hint,
  placeholder,
  nodes,
  exclude,
  onPick,
  onClose,
  extra,
}: {
  title: string;
  hint: string;
  placeholder: string;
  nodes: NodeView[];
  exclude: Set<string>;
  onPick: (id: string | null) => Promise<void>;
  onClose: () => void;
  /** A button for the choice that isn't a node, e.g. "Move to the top level". */
  extra?: string;
}) {
  const [error, setError] = useState<string | null>(null);
  const pick = (id: string | null) => {
    setError(null);
    onPick(id).catch((err: unknown) => setError(errorText(err)));
  };
  return (
    <Modal title={title} onClose={onClose} footer={error && <span className="form-error">{error}</span>}>
      <p className="hint">{hint}</p>
      <NodePicker nodes={nodes} exclude={exclude} placeholder={placeholder} onPick={pick} autoFocus />
      {extra && (
        <p>
          <button onClick={() => pick(null)}>{extra}</button>
        </p>
      )}
    </Modal>
  );
}

export function MergeDialog({
  nodes,
  onMerge,
  onClose,
}: {
  nodes: NodeView[];
  onMerge: (target: string, others: string[], title: string) => Promise<void>;
  onClose: () => void;
}) {
  const [target, setTarget] = useState(nodes[0]!.id);
  const [title, setTitle] = useState(nodes[0]!.title);
  const { busy, error, submit } = useSubmit(() =>
    onMerge(
      target,
      nodes.map((n) => n.id).filter((id) => id !== target),
      title.trim(),
    ),
  );
  return (
    <Modal
      title={`Merge ${nodes.length} nodes into one`}
      onClose={onClose}
      footer={
        <>
          {error && <span className="form-error">{error}</span>}
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy || !title.trim()} onClick={() => void submit()}>
            Merge
          </button>
        </>
      }
    >
      <p className="hint">
        One node survives and takes on everything the others had: what is inside them, what they need, what needs
        them, and their descriptions. The others are deleted.
      </p>
      <fieldset className="field">
        <span>Keep</span>
        {nodes.map((node) => (
          <label key={node.id} className="check">
            <input
              type="radio"
              name="merge-target"
              checked={target === node.id}
              onChange={() => {
                setTarget(node.id);
                setTitle(node.title);
              }}
            />
            {node.title} <code className="handle">{node.handle}</code>
          </label>
        ))}
      </fieldset>
      <label className="field">
        <span>Title of the merged node</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
    </Modal>
  );
}

export function DeleteDialog({
  nodes,
  inside,
  onDelete,
  onClose,
}: {
  nodes: NodeView[];
  /** How many nodes sit inside the ones being deleted. */
  inside: number;
  onDelete: () => Promise<void>;
  onClose: () => void;
}) {
  const { busy, error, submit } = useSubmit(onDelete);
  const what = nodes.length === 1 ? `“${nodes[0]!.title}”` : `${nodes.length} nodes`;
  return (
    <Modal
      title={`Delete ${what}?`}
      onClose={onClose}
      footer={
        <>
          {error && <span className="form-error">{error}</span>}
          <button onClick={onClose}>Cancel</button>
          <button className="danger solid" disabled={busy} onClick={() => void submit()}>
            {inside ? `Delete, with the ${inside} inside` : "Delete"}
          </button>
        </>
      }
    >
      <p>
        {inside ? `This also deletes the ${inside} ${inside === 1 ? "node" : "nodes"} inside. ` : ""}
        Dependencies on what you delete are removed. The plan’s history keeps it: nothing is lost for good.
      </p>
    </Modal>
  );
}

/** Saves the plan as an `.apm.zip`; with runs, it says first what that file may hold. */
export function ExportDialog({
  projectName,
  onExport,
  onClose,
}: {
  projectName: string;
  onExport: (withRuns: boolean) => Promise<void>;
  onClose: () => void;
}) {
  const [withRuns, setWithRuns] = useState(false);
  const { busy, error, submit } = useSubmit(() => onExport(withRuns));
  return (
    <Modal
      title={`Export “${projectName}”`}
      onClose={onClose}
      footer={
        <>
          {error && <span className="form-error">{error}</span>}
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy} onClick={() => void submit()}>
            {busy ? "Preparing…" : withRuns ? "Download, with runs" : "Download"}
          </button>
        </>
      }
    >
      <p>
        Saves the plan and its full history as one <code>.apm.zip</code> file. Someone else can open it with{" "}
        <code>apm import</code>.
      </p>
      <label className="check">
        <input type="checkbox" checked={withRuns} onChange={(e) => setWithRuns(e.target.checked)} />
        Include runs: agent transcripts and proposals
      </label>
      {withRuns && (
        <p className="callout warn" role="alert">
          Run transcripts can include tool output and secrets: file contents, tokens, keys. Check the file before you
          share it.
        </p>
      )}
    </Modal>
  );
}
