"""A scripted fleet, for looking at the canvas without running real agents.

§7's Phase 2 gate asks for "blocked edges render with `blockingDependencyIds`",
but the assign/handoff graph that produces those edges is Phase 3's mailbox.
Rather than fake the rendering, this writes a real multi-agent run into the
real store, through the real event types -- so the canvas is exercised end to
end and Phase 3 replaces the *producer* without touching the UI.

Every event is stamped ``synthetic`` in its provider metadata and lands under a
run id that says so. Seeded data sharing a store with real telemetry is only
safe for as long as it cannot be mistaken for a fleet, and that is a property
of the data, not of the operator remembering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fleetview.schema.events import AgentState, EventType, FleetViewEvent

#: Run ids from the seeder are prefixed, so `fleetview events tail --run` and
#: the UI can both tell demo data apart at a glance.
DEMO_RUN_PREFIX = "demo-"

BRAIN = "demo-brain"
WORKER_CODEX = "demo-codex-1"
WORKER_CLAUDE = "demo-claude-2"


def demo_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{DEMO_RUN_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}"


def build_demo_events(run_id: str, *, now: datetime | None = None) -> list[FleetViewEvent]:
    """The §7 demo scenario, as an ordered event list.

    A Claude brain decomposes a refactor, assigns implementation to a Codex
    worker and review to a second Claude worker. One worker is stalled on a
    permission prompt; the brain is blocked on both.
    """
    start = now or datetime.now(timezone.utc)
    clock = iter(range(1000))

    def at() -> datetime:
        return start + timedelta(milliseconds=next(clock) * 120)

    def make(agent, event_type, payload=None, provider="claude") -> FleetViewEvent:
        return FleetViewEvent(
            run_id=run_id,
            agent_id=agent,
            channel="hook",
            event_type=event_type,
            payload=payload or {},
            timestamp=at(),
            provider_metadata={"provider": provider, "synthetic": True},
        )

    events = [
        make(BRAIN, EventType.AGENT_READY),
        make(BRAIN, EventType.TASK_STARTED, {"prompt": "Refactor auth and review it"}),

        make(WORKER_CODEX, EventType.AGENT_READY, provider="codex"),
        make(WORKER_CLAUDE, EventType.AGENT_READY),

        # §3.1's reason to exist: a Claude brain handing work to a Codex worker,
        # each authenticating on its own subscription.
        make(BRAIN, EventType.MESSAGE_SENT, {
            "to": WORKER_CODEX, "from": BRAIN, "orchestration": "assign",
            "act": "request", "subject": "implement the refactor",
            "conversation": "conv-1",
        }),
        make(BRAIN, EventType.MESSAGE_SENT, {
            "to": WORKER_CLAUDE, "from": BRAIN, "orchestration": "assign",
            "act": "request", "subject": "review the refactor",
            "conversation": "conv-2",
        }),

        # The brain parks. `assign` is async, so this is an explicit state
        # change rather than something the send implies (§6 clause 3).
        make(BRAIN, EventType.TERMINAL_STATUS_CHANGED, {
            "status": AgentState.WAITING_ON_SUBAGENT,
            "blocking_dependency_ids": [WORKER_CODEX, WORKER_CLAUDE],
        }),

        # The Codex worker is getting on with it.
        make(WORKER_CODEX, EventType.TASK_STARTED,
             {"prompt": "implement the refactor"}, provider="codex"),
        make(WORKER_CODEX, EventType.TERMINAL_TAP_OPENED, provider="codex"),
        make(WORKER_CODEX, EventType.TOOL_REQUESTED,
             {"tool_name": "Edit", "tool_input": {"path": "src/auth.py"}}, provider="codex"),
        make(WORKER_CODEX, EventType.TOOL_RESULT, {"tool_name": "Edit"}, provider="codex"),
        make(WORKER_CODEX, EventType.TOOL_REQUESTED,
             {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, provider="codex"),

        # The Claude worker is stalled on the CLI's own permission prompt --
        # §3.5.1's layered gate doing exactly what it is for, and the state
        # that jumps the queue in the UI (§5.3).
        make(WORKER_CLAUDE, EventType.TASK_STARTED, {"prompt": "review the refactor"}),
        make(WORKER_CLAUDE, EventType.TERMINAL_TAP_OPENED),
        make(WORKER_CLAUDE, EventType.HUMAN_INTERVENTION_REQUESTED, {
            "blocked_state": AgentState.WAITING_ON_HUMAN,
            "message": "Claude needs your permission to use Bash",
        }),
    ]
    return events


def build_quota_wall(
    run_id: str, agent: str = WORKER_CODEX, *, now: datetime | None = None
) -> list[FleetViewEvent]:
    """The subscription failure mode (§5.3, §9 risk 2).

    Under a plan the dominant failure is quota exhaustion, not cost overrun --
    and it is indistinguishable from a hang unless the UI names it. FleetView
    parks rather than retrying (§8.5 requirement 2), so this is a *state*, not
    a transient error.
    """
    # After the scripted run, not merely "now": the default timestamp is the
    # moment this function is called, which is *earlier* than the offsets
    # `build_demo_events` projects forward -- so the worker would show a
    # last-seen time before its own tool call.
    when = (now or datetime.now(timezone.utc)) + timedelta(seconds=30)
    return [
        FleetViewEvent(
            run_id=run_id, agent_id=agent, channel="daemon",
            event_type=EventType.QUOTA_EXHAUSTED,
            timestamp=when,
            payload={"provider": "codex", "resets_at": "in about 40 minutes"},
            provider_metadata={"provider": "codex", "synthetic": True},
        )
    ]
