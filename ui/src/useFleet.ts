import { useCallback, useEffect, useRef, useState } from "react";
import type { FleetSnapshot, FleetViewEvent, LiveFrame } from "./types";

const EMPTY: FleetSnapshot = {
  runId: null,
  generatedAt: new Date().toISOString(),
  maxConcurrentAgents: 0,
  agents: [],
  edges: [],
};

/** How many events the waterfall keeps. The durable copy is in SQLite, and
 *  `/v1/events` can page back through it; this is only what is on screen. */
const WATERFALL_DEPTH = 400;

export type Connection = "connecting" | "live" | "offline";

/**
 * The canvas reads `/ws/live` and nothing else.
 *
 * §4.1 tier 3: the daemon's in-memory projection is authoritative for the UI
 * and the database is durability, never the render path. So the socket hands
 * over whole snapshots and this hook *replaces* state rather than merging into
 * it -- a client that merges can drift out of step with the daemon, and a
 * canvas that quietly disagrees with the fleet is worse than no canvas.
 */
export function useFleet() {
  const [snapshot, setSnapshot] = useState<FleetSnapshot>(EMPTY);
  const [events, setEvents] = useState<FleetViewEvent[]>([]);
  const [connection, setConnection] = useState<Connection>("connecting");
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<number>(0);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws/live`);
    socketRef.current = socket;

    socket.onopen = () => {
      retryRef.current = 0;
      setConnection("live");
    };
    socket.onmessage = (message) => {
      const frame = JSON.parse(message.data) as LiveFrame;
      if (frame.type === "snapshot") {
        setSnapshot(frame.snapshot);
      } else {
        setEvents((previous) => [frame.event, ...previous].slice(0, WATERFALL_DEPTH));
      }
    };
    socket.onclose = () => {
      setConnection("offline");
      // The daemon restarting is ordinary -- tmux keeps the agents alive
      // across it (§4) -- so reconnect rather than asking for a refresh.
      // Backed off, because a tight loop against a daemon that is genuinely
      // gone is just noise in its log.
      const delay = Math.min(1000 * 2 ** retryRef.current, 10_000);
      retryRef.current += 1;
      window.setTimeout(connect, delay);
    };
    socket.onerror = () => socket.close();
  }, []);

  useEffect(() => {
    connect();
    return () => {
      const socket = socketRef.current;
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [connect]);

  return { snapshot, events, connection };
}
