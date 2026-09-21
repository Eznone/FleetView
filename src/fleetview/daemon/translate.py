"""Turning a vendor hook payload into a FleetView event (§5.2).

Kept as a pure function with no I/O so the mapping can be tested against
recorded payloads. It is also the layer that will absorb provider differences:
Codex's hook payload is already Claude-shaped (§2.5), while a future provider
may not be, and the adapter belongs here rather than in the ingest path.
"""

from __future__ import annotations

import re
from typing import Any

from fleetview.schema.events import AgentState, EventType, FleetViewEvent

#: §5.3 detects `waiting_on_human` from "Notification matching
#: permission/approve/confirm" — *matching*, not "any Notification". The
#: distinction is not pedantry. Observed live on 2026-09-21: a plain idle turn
#: emits `Notification: "Claude is waiting for your input"`, and a blanket
#: mapping would raise a red-amber "human needed, jumps the queue" badge for
#: every idle agent in the fleet. The state that means "someone must act now"
#: has to stay rare, or it stops meaning anything.
PERMISSION_NOTIFICATION_RE = re.compile(
    r"permission|approve|approval|confirm|authoriz|trust this|allow", re.IGNORECASE
)

#: Claude Code's hook surface (§3.4). `Stop` maps to a status change rather
#: than a task completion because a `Stop` means the agent's turn ended — which
#: is `idle` in the §5.3 taxonomy, and says nothing about whether the task it
#: was given is done.
HOOK_EVENT_TYPES: dict[str, EventType] = {
    "SessionStart": EventType.AGENT_READY,
    "UserPromptSubmit": EventType.TASK_STARTED,
    "PreToolUse": EventType.TOOL_REQUESTED,
    "PostToolUse": EventType.TOOL_RESULT,
    "Notification": EventType.HUMAN_INTERVENTION_REQUESTED,
    "Stop": EventType.TERMINAL_STATUS_CHANGED,
    "SubagentStop": EventType.TASK_COMPLETED,
    "PostCompact": EventType.CONTEXT_COMPACTED,
}

#: Payload keys worth promoting out of the vendor blob into our own payload.
#: Everything else is preserved under providerMetadata rather than discarded —
#: AgentScope's contribution, promoted in §5.1.
_PROMOTED = (
    "tool_name", "tool_input", "tool_response", "message", "prompt", "trigger",
    "last_assistant_message",
)

#: A SubagentStop's ``agent_id`` is the *subagent's* id, not ours. Renamed on
#: the way in, because two different things called agentId in one payload is
#: how a consumer ends up attributing a subagent's work to the worker that
#: spawned it.
_SUBAGENT_FIELDS = {"agent_id": "subagent_id", "agent_type": "subagent_type"}


def translate(envelope: dict[str, Any], *, default_run_id: str) -> FleetViewEvent | None:
    """Build an event from a shim envelope, or None if the hook is unknown.

    Returning None rather than raising is deliberate: a CLI update that adds a
    hook type must not make the daemon reject the whole payload. It is logged
    and dropped, and the agent never notices.
    """
    hook = envelope.get("hook") or {}
    name = hook.get("hook_event_name")
    event_type = HOOK_EVENT_TYPES.get(name)
    if event_type is None:
        return None

    payload: dict[str, Any] = {
        key: hook[key] for key in _PROMOTED if key in hook
    }
    if name == "Stop":
        payload["status"] = AgentState.IDLE

    if name == "Notification":
        # See PERMISSION_NOTIFICATION_RE. Only a permission-shaped notification
        # is an intervention request; everything else is the agent reporting
        # that it has gone quiet.
        if PERMISSION_NOTIFICATION_RE.search(str(hook.get("message", ""))):
            payload["blocked_state"] = AgentState.WAITING_ON_HUMAN
        else:
            event_type = EventType.TERMINAL_STATUS_CHANGED
            payload["status"] = AgentState.IDLE

    if name == "SubagentStop":
        # Phase 0/1 finding (2026-09-21): SubagentStop fires for Claude Code's
        # OWN internal subagents, not only for work we delegated — it was
        # observed on a plain single-turn edit with no delegation at all, from
        # the CLI's prompt-suggestion agent. So this event means "a subagent
        # finished", never "the worker's task finished", and a consumer that
        # conflates the two will show work completed that nobody asked for.
        # Carrying the subagent's identity is what keeps the two separable.
        for source, destination in _SUBAGENT_FIELDS.items():
            if source in hook:
                payload[destination] = hook[source]
        payload["is_delegated"] = bool(hook.get("agent_type"))

    # The vendor's own session id is what channel C keys transcripts by, so it
    # is worth carrying even though we identify agents by FLEETVIEW_AGENT_ID.
    provider_metadata = {
        "provider": envelope.get("provider", "claude"),
        "hookEventName": name,
        "raw": hook,
    }

    return FleetViewEvent(
        run_id=envelope.get("runId") or default_run_id,
        agent_id=envelope.get("agentId"),
        channel="hook",
        event_type=event_type,
        payload=payload,
        provider_metadata=provider_metadata,
    )
