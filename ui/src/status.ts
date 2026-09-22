import type { AgentState, AgentView } from "./types";

/**
 * PROJECT_PLAN.md §5.3, encoded rather than paraphrased.
 *
 * Three rows are not merely colours and it is worth saying why, because the
 * obvious simplification -- "blocked is amber" -- destroys all three:
 *
 *  - `waiting_on_human` **jumps the queue**. A human is the only thing that can
 *    clear it, and the whole point of the tool is that such a stall is visible
 *    and answerable rather than silent.
 *  - `waiting_on_inbox` is **not a status**. It is the signature of the
 *    delivery bug MD's wake watchdog exists to correct, so it renders as a
 *    fault; drawing it as a status makes the bug latent again.
 *  - `waiting_on_workspace_trust` is a **setup error**, not operation. Phase 0
 *    found the trust dialog's default answer is "No, exit", so an agent parked
 *    here never reaches its task at all.
 */
export type Severity = "ok" | "busy" | "blocked" | "urgent" | "fault" | "setup" | "dead";

export interface StatusStyle {
  label: string;
  severity: Severity;
  /** Higher sorts first in the fleet tree and the blocked list. */
  priority: number;
  hint?: string;
}

export const STATUS: Record<AgentState, StatusStyle> = {
  waiting_on_human: {
    label: "needs you",
    severity: "urgent",
    priority: 100,
    hint: "Answer the prompt in the agent's own pane — FleetView writes no prompt UI.",
  },
  waiting_on_workspace_trust: {
    label: "trust not granted",
    severity: "setup",
    priority: 95,
    hint: "Setup error: confirm the working directory once, then respawn.",
  },
  waiting_on_inbox: {
    label: "undrained mail",
    severity: "fault",
    priority: 90,
    hint: "Not a status — idle with mail waiting is a delivery fault.",
  },
  failed: { label: "failed", severity: "dead", priority: 85 },
  rate_limited: {
    label: "rate limited",
    severity: "fault",
    priority: 80,
    hint: "Parked until the window resets. FleetView never retries through a quota wall.",
  },
  waiting_on_subagent: { label: "waiting on workers", severity: "blocked", priority: 60 },
  waiting_on_tool: { label: "waiting on tool", severity: "blocked", priority: 50 },
  running: { label: "running", severity: "busy", priority: 40 },
  idle: { label: "idle", severity: "ok", priority: 10 },
};

export function styleFor(agent: AgentView): StatusStyle {
  return STATUS[agent.state] ?? { label: agent.state, severity: "ok", priority: 0 };
}

export function describe(agent: AgentView): string {
  if (agent.state === "waiting_on_tool" && agent.currentTool) {
    return `waiting on ${agent.currentTool}`;
  }
  if (agent.state === "running" && agent.currentTool) {
    return `running ${agent.currentTool}`;
  }
  if (agent.state === "waiting_on_subagent" && agent.blockingDependencyIds.length) {
    return `waiting on ${agent.blockingDependencyIds.length} worker(s)`;
  }
  return styleFor(agent).label;
}

/** Queue-jumping order: the agent that most needs a human comes first. */
export function byUrgency(a: AgentView, b: AgentView): number {
  const delta = styleFor(b).priority - styleFor(a).priority;
  return delta !== 0 ? delta : a.agentId.localeCompare(b.agentId);
}
