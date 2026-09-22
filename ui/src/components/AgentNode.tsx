import { Handle, Position } from "@xyflow/react";
import type { AgentView } from "../types";
import { describe, styleFor } from "../status";

export function AgentNode({ data }: { data: { agent: AgentView; selected: boolean } }) {
  const { agent } = data;
  const style = styleFor(agent);

  return (
    <div
      className={`node sev-${style.severity} ${data.selected ? "on" : ""} ${
        agent.terminated ? "gone" : ""
      }`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="node-head">
        <span className="node-name">{agent.agentId}</span>
        {agent.synthetic && <span className="tag">demo</span>}
      </div>
      <div className="node-state">
        <i className="dot" />
        {describe(agent)}
      </div>
      <div className="node-foot">
        <span>{agent.provider ?? "—"}</span>
        {agent.terminalAttached && <span title="pane captured">▮</span>}
        <span>{agent.eventCount} ev</span>
      </div>
      {style.hint && style.severity !== "busy" && (
        <div className="node-hint">{style.hint}</div>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
