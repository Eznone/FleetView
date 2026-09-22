import type { AgentView } from "../types";
import { describe, styleFor } from "../status";

export function StatusBadge({ agent, detail = true }: { agent: AgentView; detail?: boolean }) {
  const style = styleFor(agent);
  return (
    <span className={`badge sev-${style.severity}`} title={style.hint ?? style.label}>
      <i className="dot" />
      {detail ? describe(agent) : style.label}
    </span>
  );
}
