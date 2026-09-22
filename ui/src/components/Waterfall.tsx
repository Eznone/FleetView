import { useState } from "react";
import type { FleetViewEvent } from "../types";

/** §5.6's collapsible bottom drawer. Deliberately thin in Phase 2: the span
 *  waterfall it will become needs the delegation graph Phase 3 builds. */
export function Waterfall({
  events,
  onSelect,
}: {
  events: FleetViewEvent[];
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <footer className={`waterfall ${open ? "open" : ""}`}>
      <button className="waterfall-toggle" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} Event log
        <span className="count">{events.length}</span>
      </button>
      {open && (
        <div className="waterfall-body">
          {events.length === 0 && <p className="empty">Nothing yet.</p>}
          {events.map((event) => (
            <button
              key={event.id}
              className={`row chan-${event.channel}`}
              onClick={() => event.agentId && onSelect(event.agentId)}
            >
              <span className="when">
                {new Date(event.timestamp).toLocaleTimeString()}
              </span>
              <span className="who">{event.agentId ?? "daemon"}</span>
              <span className="what mono">{event.eventType}</span>
              <span className="detail">
                {String(event.payload.tool_name ?? event.payload.message ?? event.payload.prompt ?? "")}
              </span>
            </button>
          ))}
        </div>
      )}
    </footer>
  );
}
