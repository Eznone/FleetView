import type { AgentView } from "../types";
import { byUrgency, styleFor } from "../status";
import { StatusBadge } from "./StatusBadge";

/**
 * The left pane of §5.6's tri-pane layout.
 *
 * Sorted by urgency rather than by name, which is what "jumps the queue"
 * (§5.3) means in practice: the agent a human must unblock is at the top of
 * the list whatever it is called.
 */
export function FleetTree({
  agents,
  selected,
  onSelect,
}: {
  agents: AgentView[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const ordered = [...agents].sort(byUrgency);
  const blocked = ordered.filter((a) => styleFor(a).priority >= 60 && !a.terminated);

  return (
    <aside className="pane pane-left">
      <h2>Fleet</h2>
      {ordered.length === 0 && (
        <p className="empty">
          No agents yet. Spawn one with <code>fleetview spike claude --hooks</code>, or
          seed a scripted fleet with <code>fleetview demo seed</code>.
        </p>
      )}

      {blocked.length > 0 && (
        <div className="blocked-list">
          <h3>Blocked</h3>
          {blocked.map((agent) => (
            <button
              key={agent.agentId}
              className="blocked-row"
              onClick={() => onSelect(agent.agentId)}
              title={styleFor(agent).hint}
            >
              <StatusBadge agent={agent} detail={false} />
              <span className="name">{agent.agentId}</span>
            </button>
          ))}
        </div>
      )}

      <ul className="tree">
        {ordered.map((agent) => (
          <li key={agent.agentId}>
            <button
              className={`tree-row ${selected === agent.agentId ? "on" : ""} ${
                agent.terminated ? "gone" : ""
              }`}
              onClick={() => onSelect(agent.agentId)}
            >
              <span className="name">{agent.agentId}</span>
              <StatusBadge agent={agent} />
              <span className="meta">
                {agent.provider ?? "—"}
                {agent.terminalAttached ? " · pane" : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
