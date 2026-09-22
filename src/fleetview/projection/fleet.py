"""The CQRS read side: events in, canvas out (PROJECT_PLAN.md §5.4).

`apply` is a **pure fold**. It performs no I/O, reads no clock and touches no
database, so replaying the log rebuilds exactly the state a live daemon holds.
That equivalence is the whole justification for event sourcing here (§4.1's
escape hatch) and `test_rebuild_from_the_log_equals_the_live_projection`
exists to keep it true -- it is cheap to preserve and expensive to recover.

Two of Claude Code's hooks mean something other than what their names suggest,
both found on 2026-09-21 (`PHASE_1.md`), and a reducer written from the event
names alone re-breaks both. They are handled in `_subagent_stop` and
`_human_intervention`, each with the finding written beside it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone

from fleetview.config import Settings
from fleetview.projection.state import AgentView, FleetEdge, FleetSnapshot
from fleetview.schema.events import AgentState, EventType, FleetViewEvent

log = logging.getLogger(__name__)

#: What the canvas actually draws. `apply` reports "changed" against *this*
#: tuple rather than against the whole view, because `event_count` and
#: `last_event_at` move on every single event -- a change signal that is always
#: true would push a full agent delta per event and make the UI's own
#: coalescing pointless.
def _render_fingerprint(view: AgentView) -> tuple:
    return (
        view.state,
        view.current_tool,
        tuple(view.blocking_dependency_ids),
        view.terminal_attached,
        view.terminated,
        view.tmux_session,
        view.provider,
        view.last_message,
        view.synthetic,
    )


def _as_state(value: object) -> AgentState | None:
    """Coerce a payload's status field, tolerating both spellings.

    Live, a payload carries an `AgentState` member straight from
    :mod:`fleetview.daemon.translate`. Replayed, it has been through
    `json.dumps`/`loads` and is a plain string. `AgentState` is a `StrEnum` so
    the two compare equal, but only *after* coercion -- and a projection that
    rebuilt differently from the way it ran live would be a silent divergence
    of exactly the kind this codebase keeps finding. Unknown values return
    None rather than raising: a CLI update that invents a status must not take
    the canvas down.
    """
    if value is None:
        return None
    try:
        return AgentState(str(value))
    except ValueError:
        log.info("ignoring unknown agent state: %r", value)
        return None


class FleetProjection:
    """Live fleet state, folded from the event log."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        run_id: str | None = None,
    ) -> None:
        self._settings = settings or Settings()
        self._run_id = run_id
        self._agents: dict[str, AgentView] = {}
        self._edges: dict[str, FleetEdge] = {}

    # --- the fold ----------------------------------------------------------

    def apply(self, event: FleetViewEvent) -> bool:
        """Fold one event in. True when something the canvas renders moved."""
        if event.agent_id is None:
            # Daemon-level bookkeeping with no agent to attribute it to.
            return False

        view = self._agents.get(event.agent_id)
        if view is None:
            view = AgentView(agent_id=event.agent_id, run_id=event.run_id)
            self._agents[event.agent_id] = view
            before = None
        else:
            before = _render_fingerprint(view)

        view.event_count += 1
        view.last_event_at = event.timestamp
        if event.run_id:
            view.run_id = event.run_id

        metadata = event.provider_metadata or {}
        if metadata.get("provider"):
            view.provider = str(metadata["provider"])
        if metadata.get("synthetic"):
            view.synthetic = True

        self._dispatch(view, event)

        after = _render_fingerprint(view)
        return before is None or before != after

    def _dispatch(self, view: AgentView, event: FleetViewEvent) -> None:
        kind = event.event_type
        payload = event.payload

        # A status may ride along with any event that carries one, and so may
        # the dependency list behind `waiting_on_subagent` (§7's blocked edge).
        if "blocking_dependency_ids" in payload:
            view.blocking_dependency_ids = [str(x) for x in payload["blocking_dependency_ids"]]

        if kind is EventType.AGENT_READY:
            view.state = AgentState.IDLE
            view.terminated = False
        elif kind is EventType.TASK_STARTED:
            view.state = AgentState.RUNNING
            if payload.get("prompt"):
                view.last_message = str(payload["prompt"])
        elif kind is EventType.TOOL_REQUESTED:
            view.state = AgentState.RUNNING
            view.current_tool = payload.get("tool_name")
            view.current_tool_span = event.span_id
            view.current_tool_started_at = event.timestamp
        elif kind is EventType.TOOL_RESULT:
            view.current_tool = None
            view.current_tool_span = None
            view.current_tool_started_at = None
            # Only lift a *tool* block. An agent that went to waiting_on_human
            # mid-call is still waiting on the human.
            if view.state in (AgentState.WAITING_ON_TOOL, AgentState.RUNNING):
                view.state = AgentState.RUNNING
        elif kind is EventType.HUMAN_INTERVENTION_REQUESTED:
            self._human_intervention(view, payload)
        elif kind is EventType.HUMAN_INTERVENTION_RESOLVED:
            view.state = AgentState.RUNNING
        elif kind is EventType.TERMINAL_STATUS_CHANGED:
            state = _as_state(payload.get("status"))
            if state is not None:
                view.state = state
        elif kind is EventType.TASK_COMPLETED:
            self._subagent_stop(view, payload)
        elif kind in (
            EventType.TERMINAL_TAP_OPENED,
            EventType.TERMINAL_RESUMED,
        ):
            view.terminal_attached = True
        elif kind in (
            EventType.TERMINAL_TAP_CLOSED,
            EventType.TERMINAL_TAP_FAILED,
        ):
            view.terminal_attached = False
        elif kind in (EventType.AGENT_TERMINATED, EventType.PROCESS_EXITED):
            # The node stays on the canvas, greyed. Its last state is left
            # alone on purpose: "it died while waiting_on_human" is the most
            # useful thing the card can say, and overwriting it with `idle`
            # throws that away.
            view.terminated = True
        elif kind is EventType.PROCESS_CRASHED:
            view.state = AgentState.FAILED
            view.terminated = True
        elif kind is EventType.QUOTA_EXHAUSTED:
            if view.state is not AgentState.RATE_LIMITED:
                view.state_before_rate_limit = view.state
            view.state = AgentState.RATE_LIMITED
        elif kind is EventType.QUOTA_RECOVERED:
            view.state = view.state_before_rate_limit or AgentState.IDLE
            view.state_before_rate_limit = None
        elif kind is EventType.MESSAGE_SENT:
            self._message_sent(view, event)
        elif kind in (
            EventType.MESSAGE_DELIVERED,
            EventType.MESSAGE_ACKNOWLEDGED,
        ):
            self._message_closed(event)

        if payload.get("tmux_session"):
            view.tmux_session = str(payload["tmux_session"])

    # --- the two hooks that do not mean what they are called ---------------

    def _human_intervention(self, view: AgentView, payload: dict) -> None:
        """`waiting_on_human` comes from `blocked_state`, never from the type.

        §5.3 detects it from a `Notification` *matching*
        permission/approve/confirm, and :mod:`fleetview.daemon.translate`
        already does that matching -- a plain idle turn emits
        `Notification: "Claude is waiting for your input"` and is demoted to an
        ordinary idle status change before it ever reaches here. Keying off the
        event type instead would raise the red-amber, queue-jumping badge on
        every idle agent in the fleet, and a state that means "someone must act
        now" stops meaning anything once it is always on.
        """
        blocked = _as_state(payload.get("blocked_state"))
        if blocked is not None:
            view.state = blocked
        if payload.get("message"):
            view.last_message = str(payload["message"])

    def _subagent_stop(self, view: AgentView, payload: dict) -> None:
        """`SubagentStop` is not "the worker's task finished".

        It fires for Claude Code's *own* internal subagents -- observed on a
        plain single-turn edit with no delegation at all (`PHASE_1.md`). Its id
        arrives already renamed to `subagent_id` by `translate`, precisely so
        the two cannot be conflated. Counting it is fine; moving the agent's
        state is not, because the canvas would report work nobody asked for,
        and Phase 3's assign/handoff graph would grow edges from the CLI's
        internal machinery.
        """
        view.subagent_count += 1
        if payload.get("subagent_type"):
            view.last_message = f"subagent {payload['subagent_type']} finished"

    # --- edges (§5.5; seeded in Phase 2, real in Phase 3) ------------------

    def _message_sent(self, view: AgentView, event: FleetViewEvent) -> None:
        payload = event.payload
        target = payload.get("to")
        if not target:
            return
        source = str(payload.get("from") or event.agent_id)
        orchestration = str(payload.get("orchestration") or "send_message")
        if orchestration not in ("assign", "handoff", "send_message"):
            orchestration = "send_message"

        edge = FleetEdge(
            id=f"{source}->{target}:{orchestration}",
            source=source,
            target=str(target),
            orchestration=orchestration,  # type: ignore[arg-type]
            conversation=payload.get("conversation"),
            created_at=event.timestamp,
        )
        self._edges[edge.id] = edge

        if orchestration == "handoff":
            # §6 clause 3: handoff is synchronous, so the sender is blocked on
            # the result. `assign` is async and deliberately does not block --
            # a supervisor that waits on an assign deadlocks the fleet.
            view.state = AgentState.WAITING_ON_SUBAGENT
            if str(target) not in view.blocking_dependency_ids:
                view.blocking_dependency_ids = [
                    *view.blocking_dependency_ids, str(target)
                ]

    def _message_closed(self, event: FleetViewEvent) -> None:
        target = event.payload.get("to")
        source = str(event.payload.get("from") or event.agent_id)
        for edge in self._edges.values():
            if edge.source == source and (target is None or edge.target == str(target)):
                edge.state = "closed"

    # --- the one transition no event announces -----------------------------

    def tick(self, now: datetime | None = None) -> bool:
        """Promote a stalled tool call to `waiting_on_tool` (§5.3).

        The taxonomy defines this state as "`PreToolUse` with no `PostToolUse`
        past threshold" -- it is a property of *elapsed time*, so no event will
        ever deliver it and something has to look at the clock. That is why
        this is a separate method on a separate task: §4.1 requires pruning and
        anything else periodic to stay off the ingest path, and this is the
        same rule.
        """
        now = now or datetime.now(timezone.utc)
        threshold = self._settings.tool_stall_threshold_ms / 1000.0
        changed = False
        for view in self._agents.values():
            if view.state is not AgentState.RUNNING or view.current_tool_started_at is None:
                continue
            if (now - view.current_tool_started_at).total_seconds() >= threshold:
                view.state = AgentState.WAITING_ON_TOOL
                changed = True
        return changed

    # --- reading -----------------------------------------------------------

    def snapshot(self) -> FleetSnapshot:
        return FleetSnapshot(
            run_id=self._run_id,
            generated_at=datetime.now(timezone.utc),
            max_concurrent_agents=self._settings.max_concurrent_active_agents,
            agents=[v.model_copy(deep=True) for v in self._agents.values()],
            edges=[e.model_copy(deep=True) for e in self._edges.values()],
        )

    def agent(self, agent_id: str) -> AgentView | None:
        return self._agents.get(agent_id)

    def rebuild(self, events: Iterable[FleetViewEvent]) -> None:
        """Replay the log from cold. Must land where `apply` would have."""
        self._agents.clear()
        self._edges.clear()
        for event in events:
            self.apply(event)

    @property
    def agent_count(self) -> int:
        return sum(1 for v in self._agents.values() if not v.terminated)
