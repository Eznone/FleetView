"""The read side (PROJECT_PLAN.md §5.4) -- what the canvas is told.

Two of these tests guard Phase 1 findings rather than code: `SubagentStop` and
a plain idle `Notification` both mean something other than their names, and a
reducer written from the event vocabulary alone gets both wrong in a way that
looks like it works. See `PHASE_1.md`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fleetview.config import Settings
from fleetview.daemon.translate import translate
from fleetview.projection import FleetProjection
from fleetview.schema.events import AgentState, EventType, FleetViewEvent

RUN = "run-1"


def event(event_type, *, agent="a1", payload=None, at=None, **kwargs) -> FleetViewEvent:
    return FleetViewEvent(
        run_id=RUN,
        agent_id=agent,
        channel="hook",
        event_type=event_type,
        payload=payload or {},
        timestamp=at or datetime.now(timezone.utc),
        **kwargs,
    )


def hook(name: str, *, agent="a1", **fields) -> FleetViewEvent:
    """Go through the real translator, so the payload shape is the real one."""
    envelope = {"agentId": agent, "runId": RUN, "hook": {"hook_event_name": name, **fields}}
    translated = translate(envelope, default_run_id=RUN)
    assert translated is not None
    return translated


@pytest.fixture
def fleet() -> FleetProjection:
    return FleetProjection(settings=Settings(), run_id=RUN)


# --- the §5.3 taxonomy --------------------------------------------------------


def test_session_start_puts_a_new_agent_on_the_canvas(fleet):
    assert fleet.apply(hook("SessionStart")) is True
    assert fleet.agent("a1").state is AgentState.IDLE
    assert fleet.agent_count == 1


def test_a_prompt_makes_the_agent_running(fleet):
    fleet.apply(hook("SessionStart"))
    fleet.apply(hook("UserPromptSubmit", prompt="refactor auth"))
    view = fleet.agent("a1")
    assert view.state is AgentState.RUNNING
    assert view.last_message == "refactor auth"


def test_a_tool_call_is_named_while_it_is_in_flight(fleet):
    fleet.apply(hook("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
    view = fleet.agent("a1")
    assert view.state is AgentState.RUNNING
    assert view.current_tool == "Bash"
    assert view.current_tool_started_at is not None

    fleet.apply(hook("PostToolUse", tool_name="Bash"))
    assert fleet.agent("a1").current_tool is None


def test_a_stalled_tool_becomes_waiting_on_tool_only_after_the_threshold(fleet):
    start = datetime.now(timezone.utc)
    fleet.apply(event(EventType.TOOL_REQUESTED, payload={"tool_name": "Bash"}, at=start))

    threshold = timedelta(milliseconds=Settings().tool_stall_threshold_ms)
    assert fleet.tick(start + threshold - timedelta(milliseconds=1)) is False
    assert fleet.agent("a1").state is AgentState.RUNNING

    assert fleet.tick(start + threshold) is True
    assert fleet.agent("a1").state is AgentState.WAITING_ON_TOOL


def test_a_tool_result_lifts_the_tool_block(fleet):
    start = datetime.now(timezone.utc)
    fleet.apply(event(EventType.TOOL_REQUESTED, payload={"tool_name": "Bash"}, at=start))
    fleet.tick(start + timedelta(seconds=30))
    assert fleet.agent("a1").state is AgentState.WAITING_ON_TOOL

    fleet.apply(hook("PostToolUse", tool_name="Bash"))
    assert fleet.agent("a1").state is AgentState.RUNNING


def test_stop_means_the_turn_ended_not_that_the_task_finished(fleet):
    fleet.apply(hook("UserPromptSubmit", prompt="go"))
    fleet.apply(hook("Stop"))
    assert fleet.agent("a1").state is AgentState.IDLE


def test_a_crash_is_terminal_and_red(fleet):
    fleet.apply(hook("SessionStart"))
    fleet.apply(event(EventType.PROCESS_CRASHED))
    view = fleet.agent("a1")
    assert view.state is AgentState.FAILED
    assert view.terminated is True
    assert fleet.agent_count == 0


def test_a_terminated_agent_keeps_the_state_it_died_in(fleet):
    """"It died while waiting_on_human" is the useful thing the card can say."""
    fleet.apply(hook("Notification", message="Claude needs your permission to use Bash"))
    fleet.apply(event(EventType.PROCESS_EXITED))
    view = fleet.agent("a1")
    assert view.terminated is True
    assert view.state is AgentState.WAITING_ON_HUMAN


# --- the two hooks that do not mean what they are called ---------------------


def test_a_permission_notification_raises_the_human_badge(fleet):
    fleet.apply(hook("Notification", message="Claude needs your permission to use Bash"))
    assert fleet.agent("a1").state is AgentState.WAITING_ON_HUMAN


def test_a_plain_idle_notification_does_not_raise_the_human_badge(fleet):
    """PHASE_1.md: a plain idle turn emits a Notification too.

    `waiting_on_human` jumps the queue in the UI (§5.3). If every idle agent
    raises it, the state stops distinguishing anything -- which is the whole
    reason `translate` matches on the message text rather than the hook name.
    """
    fleet.apply(hook("Notification", message="Claude is waiting for your input"))
    assert fleet.agent("a1").state is AgentState.IDLE


def test_subagent_stop_does_not_complete_the_workers_task(fleet):
    """PHASE_1.md: SubagentStop fires for Claude Code's OWN internal subagents.

    Observed on a plain single-turn edit with no delegation at all. Letting it
    move the agent's state makes the canvas report work nobody asked for.
    """
    fleet.apply(hook("UserPromptSubmit", prompt="go"))
    assert fleet.agent("a1").state is AgentState.RUNNING

    fleet.apply(hook("SubagentStop", agent_id="internal-7", agent_type="general-purpose"))
    view = fleet.agent("a1")
    assert view.state is AgentState.RUNNING, "the worker is still working"
    assert view.subagent_count == 1
    assert view.agent_id == "a1", "the subagent's id must not overwrite the worker's"


# --- quota: park, never retry (§8.5 requirement 2) ---------------------------


def test_quota_exhaustion_parks_the_agent_and_recovery_restores_it(fleet):
    fleet.apply(hook("UserPromptSubmit", prompt="go"))
    fleet.apply(event(EventType.QUOTA_EXHAUSTED))
    assert fleet.agent("a1").state is AgentState.RATE_LIMITED

    fleet.apply(event(EventType.QUOTA_RECOVERED))
    assert fleet.agent("a1").state is AgentState.RUNNING


# --- edges (§5.5, and §7's Phase 2 gate) -------------------------------------


def test_a_handoff_blocks_the_sender_on_its_worker(fleet):
    fleet.apply(event(
        EventType.MESSAGE_SENT,
        agent="brain",
        payload={"to": "codex-1", "orchestration": "handoff"},
    ))
    view = fleet.agent("brain")
    assert view.state is AgentState.WAITING_ON_SUBAGENT
    assert view.blocking_dependency_ids == ["codex-1"]
    assert [e.target for e in fleet.snapshot().edges] == ["codex-1"]


def test_an_assign_does_not_block_the_sender(fleet):
    """§6 clause 3: assign is async. A supervisor that waits on one deadlocks."""
    fleet.apply(hook("UserPromptSubmit", agent="brain", prompt="go"))
    fleet.apply(event(
        EventType.MESSAGE_SENT,
        agent="brain",
        payload={"to": "codex-1", "orchestration": "assign"},
    ))
    assert fleet.agent("brain").state is AgentState.RUNNING
    assert fleet.snapshot().edges[0].orchestration == "assign"


def test_blocking_dependency_ids_ride_in_on_an_explicit_status(fleet):
    fleet.apply(event(
        EventType.TERMINAL_STATUS_CHANGED,
        agent="brain",
        payload={
            "status": AgentState.WAITING_ON_SUBAGENT,
            "blocking_dependency_ids": ["codex-1", "claude-2"],
        },
    ))
    view = fleet.agent("brain")
    assert view.state is AgentState.WAITING_ON_SUBAGENT
    assert view.blocking_dependency_ids == ["codex-1", "claude-2"]


def test_delivery_closes_the_edge(fleet):
    fleet.apply(event(EventType.MESSAGE_SENT, agent="brain",
                      payload={"to": "codex-1", "orchestration": "assign"}))
    fleet.apply(event(EventType.MESSAGE_DELIVERED, agent="brain", payload={"to": "codex-1"}))
    assert fleet.snapshot().edges[0].state == "closed"


# --- the terminal plane's lifecycle, which carries no bytes ------------------


def test_the_tap_lifecycle_flags_the_node(fleet):
    fleet.apply(event(EventType.TERMINAL_TAP_OPENED, payload={"path": "/x"}))
    assert fleet.agent("a1").terminal_attached is True
    fleet.apply(event(EventType.TERMINAL_TAP_CLOSED))
    assert fleet.agent("a1").terminal_attached is False


# --- the invariants ----------------------------------------------------------


def test_rebuild_from_the_log_equals_the_live_projection():
    """The event-sourcing invariant §4.1's escape hatch depends on.

    Cheap to keep and expensive to recover: if a replay lands anywhere other
    than where the live fold did, every projection rebuilt after a restart is
    quietly a different fleet from the one the operator was watching.
    """
    log = [
        hook("SessionStart"),
        hook("UserPromptSubmit", prompt="refactor auth"),
        hook("PreToolUse", tool_name="Edit"),
        hook("PostToolUse", tool_name="Edit"),
        hook("Notification", message="Claude needs your permission to use Bash"),
        hook("SubagentStop", agent_id="internal-7", agent_type="general-purpose"),
        event(EventType.MESSAGE_SENT, agent="brain",
              payload={"to": "a1", "orchestration": "handoff"}),
        hook("Stop"),
    ]

    live = FleetProjection(settings=Settings(), run_id=RUN)
    for item in log:
        live.apply(item)

    replayed = FleetProjection(settings=Settings(), run_id=RUN)
    replayed.rebuild(log)

    assert replayed.snapshot().agents == live.snapshot().agents
    assert replayed.snapshot().edges == live.snapshot().edges


def test_a_status_survives_the_round_trip_through_json():
    """Live, a payload holds an AgentState; replayed, it holds a plain string.

    They compare equal because AgentState is a StrEnum -- but only after
    coercion. A projection that rebuilt differently from the way it ran live
    would diverge silently, which is this project's signature bug shape.
    """
    import json

    live = FleetProjection(settings=Settings(), run_id=RUN)
    source = hook("Stop")
    live.apply(source)

    round_tripped = source.model_copy(update={"payload": json.loads(json.dumps(source.payload))})
    assert isinstance(round_tripped.payload["status"], str)

    replayed = FleetProjection(settings=Settings(), run_id=RUN)
    replayed.apply(round_tripped)

    assert replayed.agent("a1").state is live.agent("a1").state is AgentState.IDLE


def test_an_unknown_status_is_ignored_rather_than_fatal(fleet):
    """A CLI update that invents a status must not take the canvas down."""
    fleet.apply(hook("UserPromptSubmit", prompt="go"))
    fleet.apply(event(EventType.TERMINAL_STATUS_CHANGED, payload={"status": "vibing"}))
    assert fleet.agent("a1").state is AgentState.RUNNING


def test_an_event_with_no_agent_changes_nothing(fleet):
    daemon_event = FleetViewEvent(
        run_id=RUN, channel="daemon", event_type=EventType.POLICY_RULE_UPDATED,
    )
    assert fleet.apply(daemon_event) is False
    assert fleet.snapshot().agents == []


def test_a_repeated_event_does_not_report_a_render_change(fleet):
    """`apply` reports "the canvas moved", not "an event arrived"."""
    fleet.apply(hook("UserPromptSubmit", prompt="go"))
    assert fleet.apply(hook("PreToolUse", tool_name="Bash")) is True
    assert fleet.apply(hook("PreToolUse", tool_name="Bash")) is False
    assert fleet.agent("a1").event_count == 3
