import { useState } from "react";
import type { AgentView, FleetViewEvent } from "../types";
import { styleFor } from "../status";
import { StatusBadge } from "./StatusBadge";
import { TerminalPane } from "./Terminal";

type Tab = "detail" | "pane" | "raw";

/** §5.6's right pane, including §5.1's raw-payload drawer: normalize the core
 *  fields, but never discard what the provider sent. */
export function Inspector({
  agent,
  events,
}: {
  agent: AgentView | null;
  events: FleetViewEvent[];
}) {
  const [tab, setTab] = useState<Tab>("detail");

  if (!agent) {
    return (
      <aside className="pane pane-right">
        <h2>Inspector</h2>
        <p className="empty">Select an agent to see why it is doing what it is doing.</p>
      </aside>
    );
  }

  const style = styleFor(agent);
  const mine = events.filter((e) => e.agentId === agent.agentId);

  return (
    <aside className="pane pane-right">
      <h2>{agent.agentId}</h2>
      <StatusBadge agent={agent} />
      {style.hint && <p className="hint">{style.hint}</p>}

      <nav className="tabs">
        {(["detail", "pane", "raw"] as Tab[]).map((name) => (
          <button key={name} className={tab === name ? "on" : ""} onClick={() => setTab(name)}>
            {name}
          </button>
        ))}
      </nav>

      {tab === "detail" && (
        <dl className="detail">
          <dt>provider</dt><dd>{agent.provider ?? "—"}</dd>
          <dt>run</dt><dd className="mono">{agent.runId ?? "—"}</dd>
          <dt>tool</dt><dd>{agent.currentTool ?? "—"}</dd>
          <dt>blocked on</dt>
          <dd>{agent.blockingDependencyIds.join(", ") || "—"}</dd>
          <dt>pane</dt><dd>{agent.terminalAttached ? "captured" : "not captured"}</dd>
          <dt>events</dt><dd>{agent.eventCount}</dd>
          <dt>subagents</dt><dd>{agent.subagentCount}</dd>
          <dt>last seen</dt>
          <dd>{agent.lastEventAt ? new Date(agent.lastEventAt).toLocaleTimeString() : "—"}</dd>
          <dt>last message</dt><dd>{agent.lastMessage ?? "—"}</dd>
        </dl>
      )}

      {tab === "pane" &&
        (agent.terminalAttached ? (
          <TerminalPane agentId={agent.agentId} />
        ) : (
          <p className="empty">
            No pane captured for this agent. The tap opens when the daemon
            discovers its tmux session.
          </p>
        ))}

      {tab === "raw" && (
        <div className="raw">
          {mine.length === 0 && <p className="empty">No events for this agent yet.</p>}
          {mine.slice(0, 20).map((event) => (
            <details key={event.id}>
              <summary>
                <span className="mono">{event.eventType}</span>
                <span className="when">
                  {new Date(event.timestamp).toLocaleTimeString()}
                </span>
              </summary>
              <pre>{JSON.stringify({ payload: event.payload, providerMetadata: event.providerMetadata }, null, 2)}</pre>
            </details>
          ))}
        </div>
      )}
    </aside>
  );
}
