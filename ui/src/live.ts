// The WebSocket: tells the UI when to refetch, and streams run events.

import { useEffect, useRef, useState } from "react";
import { token } from "./api";
import type { LiveEvent } from "./types";

type Listener = (event: LiveEvent) => void;
const listeners = new Set<Listener>();

/** Subscribes to live events for as long as the component is mounted. */
export function useLiveEvents(listener: Listener): void {
  const latest = useRef(listener);
  latest.current = listener;
  useEffect(() => {
    const call: Listener = (event) => latest.current(event);
    listeners.add(call);
    return () => {
      listeners.delete(call);
    };
  }, []);
}

/** Opens the socket, reconnecting when it drops. Returns whether it is connected. */
export function useLiveConnection(): boolean {
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    let socket: WebSocket | null = null;
    let closed = false;
    let retry: number | undefined;
    const connect = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws?token=${encodeURIComponent(token())}`);
      socket.onopen = () => setConnected(true);
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data) as LiveEvent;
        for (const listener of listeners) listener(event);
      };
      socket.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(connect, 1500);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, []);
  return connected;
}
