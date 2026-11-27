// The window's chrome: panels you can resize and hide, and back and forward through the levels
// you visited.

import { useCallback, useState } from "react";
import { cx } from "./util";

/** A number kept in this browser (a panel's width): falls back to `fallback` where storage is
 * unavailable, as in a private window.
 */
export function useStoredNumber(key: string, fallback: number | null): [number | null, (value: number | null) => void] {
  const [value, setValue] = useState<number | null>(() => {
    try {
      const stored = Number(localStorage.getItem(key));
      return stored > 0 ? stored : fallback;
    } catch {
      return fallback;
    }
  });
  const store = useCallback(
    (next: number | null) => {
      setValue(next);
      try {
        if (next === null) localStorage.removeItem(key);
        else localStorage.setItem(key, String(Math.round(next)));
      } catch {
        // the width lasts for this page only
      }
    },
    [key],
  );
  return [value, store];
}

/** The handle between two panels. Drag it (or focus it and press ← →) to resize; double-click
 * to go back to the default width.
 */
export function Grip({ className, label, onResize, onReset }: { className: string; label: string; onResize: (dx: number) => void; onReset: () => void }) {
  const start = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    let last = event.clientX;
    const move = (e: PointerEvent) => {
      onResize(e.clientX - last);
      last = e.clientX;
    };
    const end = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", end);
      handle.removeEventListener("pointercancel", end);
      document.body.classList.remove("resizing");
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
    document.body.classList.add("resizing");
  };
  return (
    <div
      className={cx("grip", className)}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title={`${label}: drag to resize, double-click to reset`}
      tabIndex={0}
      onPointerDown={start}
      onDoubleClick={onReset}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") onResize(-24);
        if (event.key === "ArrowRight") onResize(24);
      }}
    />
  );
}

export function SidebarIcon({ open }: { open: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.3">
      <rect x="1.5" y="2.5" width="13" height="11" rx="2.5" />
      <path d="M6 2.5v11" />
      {open && <path d="M2.8 5h1.8M2.8 7h1.8M2.8 9h1.8" strokeLinecap="round" />}
    </svg>
  );
}

// --- back and forward ---
//
// Moving to another level is a step in the browser's history; picking a node on the same level is
// not, so Back undoes level moves. Each entry carries its position, and the furthest one is kept
// for this tab, so the buttons know whether there is somewhere to go.

const LAST = "apm-nav-last";

function position(): number {
  const state = window.history.state as { apm?: number } | null;
  return typeof state?.apm === "number" ? state.apm : 0;
}

function lastPosition(): number {
  try {
    return Number(sessionStorage.getItem(LAST)) || 0;
  } catch {
    return 0;
  }
}

/** A tab that opens APM afresh starts its history here. */
export function startNav(): void {
  if (window.history.state === null || typeof (window.history.state as { apm?: number }).apm !== "number") {
    window.history.replaceState({ apm: 0 }, "", window.location.href);
    try {
      sessionStorage.setItem(LAST, "0");
    } catch {
      // nothing to forget
    }
  }
}

export interface Nav {
  back: boolean;
  forward: boolean;
}

export function navState(): Nav {
  const at = position();
  return { back: at > 0, forward: at < lastPosition() };
}

/** Goes to `hash` as a new step (`step`), or in place of the current entry. */
export function navigate(hash: string, step: boolean): void {
  const url = hash || window.location.pathname + window.location.search;
  if (!step) {
    window.history.replaceState({ apm: position() }, "", url);
    return;
  }
  const at = position() + 1;
  window.history.pushState({ apm: at }, "", url);
  try {
    sessionStorage.setItem(LAST, String(at));
  } catch {
    // Forward stays available until the tab is reloaded
  }
}

/** Sets or clears one parameter of the address without making a step: the conversation in view. */
export function setHashParam(key: string, value: string | null): void {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  if (value) params.set(key, value);
  else params.delete(key);
  const text = params.toString();
  navigate(text ? `#${text}` : "", false);
}
