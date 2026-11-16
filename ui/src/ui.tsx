// Small shared pieces: the state pill, the modal, the spinner.

import { type ReactNode, useEffect, useRef } from "react";
import type { NodeView } from "./types";
import { cx, stateOf } from "./util";

export function StatePill({ node }: { node: Pick<NodeView, "status" | "ready" | "blocked"> }) {
  const state = stateOf(node);
  return (
    <span className={cx("pill", `state-${state.key}`)}>
      <span className="dot" />
      {state.label}
    </span>
  );
}

export function Modal({
  title,
  children,
  onClose,
  footer,
  wide,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    dialog.current?.querySelector<HTMLElement>("input, textarea, select")?.focus();
    return () => window.removeEventListener("keydown", onKey);
    // Focus once, when the dialog opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={cx("modal", wide && "wide")} role="dialog" aria-modal="true" aria-label={title} ref={dialog}>
        <header>
          <h2>{title}</h2>
          <button className="icon" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" role="status" aria-label="Working" />;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}
