"""The event envelope and its vocabularies (PROJECT_PLAN.md §5).

Field names are snake_case in Python and **camelCase on the wire**, because the
schema in §5.1 is written as a TypeScript interface and the UI consumes it
directly. Pydantic's alias generator does the translation, and
``populate_by_name`` means both spellings parse — so a hook payload or a stored
row round-trips either way.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from fleetview.ids import new_event_id, new_span_id, new_trace_id

#: Which telemetry plane a fact arrived on (§3.4). This is not decoration: the
#: same logical event can arrive from the hook plane in milliseconds and the
#: transcript plane seconds later, and the UI has to be able to say which
#: source it is trusting. Without it, the later copy silently looks like a
#: second occurrence.
Channel = Literal["hook", "terminal", "transcript", "daemon"]


class EventType(StrEnum):
    """§5.2. AgentPulse's set minus the API-call events, plus CLI-process events.

    There are no `llm.api.*` types by design — there is no API to observe. The
    CLI authenticates itself and we watch its lifecycle, which is the whole
    shape of the subscription constraint.
    """

    AGENT_CREATED = "agent.lifecycle.created"
    AGENT_READY = "agent.lifecycle.ready"
    AGENT_TERMINATED = "agent.lifecycle.terminated"

    PROCESS_SPAWNED = "agent.process.spawned"
    PROCESS_EXITED = "agent.process.exited"
    PROCESS_CRASHED = "agent.process.crashed"

    TERMINAL_STATUS_CHANGED = "terminal.status_changed"

    TASK_CREATED = "task.lifecycle.created"
    TASK_STARTED = "task.lifecycle.started"
    TASK_STATE_CHANGED = "task.lifecycle.state_changed"
    TASK_COMPLETED = "task.lifecycle.completed"
    TASK_FAILED = "task.lifecycle.failed"
    TASK_RETRIED = "task.lifecycle.retried"
    TASK_CANCELLED = "task.lifecycle.cancelled"

    TOOL_REQUESTED = "tool.execution.requested"
    TOOL_INVOKED = "tool.execution.invoked"
    TOOL_RESULT = "tool.execution.result"
    #: FleetView's own rule layer blocked it; no human was ever asked.
    TOOL_POLICY_DENIED = "tool.execution.policy_denied"
    #: A human answered the CLI's *native* prompt. Distinct from the above on
    #: purpose — the two layers of §3.5.1 must stay distinguishable in the log.
    TOOL_APPROVED = "tool.execution.approved"
    TOOL_DENIED = "tool.execution.denied"

    POLICY_RULE_MATCHED = "policy.rule.matched"
    POLICY_RULE_UPDATED = "policy.rule.updated"

    MESSAGE_SENT = "message.inter_agent.sent"
    MESSAGE_DELIVERED = "message.inter_agent.delivered"
    MESSAGE_ACKNOWLEDGED = "message.inter_agent.acknowledged"

    HUMAN_INTERVENTION_REQUESTED = "human.intervention.requested"
    HUMAN_INTERVENTION_RESOLVED = "human.intervention.resolved"

    CONTEXT_COMPACTED = "context.compacted"
    CONTEXT_MEMORY_MUTATED = "context.memory_mutated"

    #: The subscription failure mode. Under a plan the dominant failure is quota
    #: exhaustion, not cost overrun.
    QUOTA_WARNING = "quota.warning"
    QUOTA_EXHAUSTED = "quota.exhausted"
    QUOTA_RECOVERED = "quota.recovered"

    LLM_TURN_COMPLETED = "llm.turn.completed"


class AgentState(StrEnum):
    """§5.3. "Why is this agent waiting?" is the question the tool exists to
    answer, so the blocked states are enumerated rather than collapsed into one
    `blocked`."""

    RUNNING = "running"
    WAITING_ON_TOOL = "waiting_on_tool"
    #: Jumps the queue in the UI — a human is the only thing that can clear it.
    WAITING_ON_HUMAN = "waiting_on_human"
    #: A setup error, not normal operation: the startup trust dialog. Phase 0
    #: found its default answer is "No, exit", so a blind wake nudge kills the
    #: agent instead of waking it.
    WAITING_ON_WORKSPACE_TRUST = "waiting_on_workspace_trust"
    WAITING_ON_SUBAGENT = "waiting_on_subagent"
    #: Not a status — the signature of a delivery bug. Rendered as a fault so
    #: it stays visible instead of latent.
    WAITING_ON_INBOX = "waiting_on_inbox"
    IDLE = "idle"
    #: Indistinguishable from a hang unless the UI names it. Never retried
    #: through: §8.5 requirement 2.
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FleetViewEvent(BaseModel):
    """The envelope every event travels in, on every channel (§5.1)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        use_enum_values=False,
        extra="forbid",
    )

    id: str = Field(default_factory=new_event_id)
    #: Monotonic per run. Left unset by producers and assigned by the store's
    #: single writer inside the transaction, so ordering is authoritative
    #: rather than dependent on which channel got there first.
    sequence: int | None = None
    timestamp: datetime = Field(default_factory=_utcnow)

    run_id: str
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=new_span_id)
    parent_span_id: str | None = None
    agent_id: str | None = None

    channel: Channel
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    #: Normalize the core fields; keep everything provider-specific here rather
    #: than discarding it. Surfaced in the UI as a raw-payload drawer.
    provider_metadata: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        """camelCase JSON-ready dict, as the UI and the hook plane expect."""
        return self.model_dump(by_alias=True, mode="json")
