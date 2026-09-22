// The wire contract, as PROJECT_PLAN.md §5.1 and §5.4 define it.
//
// One asymmetry to keep in mind everywhere below: the *envelope* is camelCase,
// because pydantic aliases it, but an event's `payload` is a free-form dict
// whose keys stay snake_case (`tool_name`, `blocked_state`, `subagent_id`).
// Both spellings are real and neither is a mistake.

/** §5.3. The blocked-state taxonomy is first-class: "why is this agent
 *  waiting?" is the question the tool exists to answer. */
export type AgentState =
  | "running"
  | "waiting_on_tool"
  | "waiting_on_human"
  | "waiting_on_workspace_trust"
  | "waiting_on_subagent"
  | "waiting_on_inbox"
  | "idle"
  | "rate_limited"
  | "failed";

export interface AgentView {
  agentId: string;
  state: AgentState;
  runId: string | null;
  provider: string | null;
  tmuxSession: string | null;
  currentTool: string | null;
  currentToolSpan: string | null;
  currentToolStartedAt: string | null;
  blockingDependencyIds: string[];
  stateBeforeRateLimit: AgentState | null;
  lastEventAt: string | null;
  eventCount: number;
  terminalAttached: boolean;
  lastMessage: string | null;
  subagentCount: number;
  terminated: boolean;
  synthetic: boolean;
}

export interface FleetEdge {
  id: string;
  source: string;
  target: string;
  orchestration: "assign" | "handoff" | "send_message";
  state: "open" | "closed";
  conversation: string | null;
  createdAt: string | null;
}

export interface FleetSnapshot {
  runId: string | null;
  generatedAt: string;
  maxConcurrentAgents: number;
  agents: AgentView[];
  edges: FleetEdge[];
}

export interface FleetViewEvent {
  id: string;
  sequence: number | null;
  timestamp: string;
  runId: string;
  traceId: string;
  spanId: string;
  parentSpanId: string | null;
  agentId: string | null;
  channel: "hook" | "terminal" | "transcript" | "daemon";
  eventType: string;
  payload: Record<string, unknown>;
  providerMetadata: Record<string, unknown> | null;
}

export type LiveFrame =
  | { type: "snapshot"; snapshot: FleetSnapshot }
  | { type: "event"; event: FleetViewEvent };
