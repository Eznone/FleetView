"""The seeded fleet.

§7's Phase 2 gate asks for blocked edges carrying `blockingDependencyIds`, but
the mailbox that produces them is Phase 3. The seeder closes that gap with real
events through the real types, so what these tests assert is that the *canvas*
is exercised honestly -- and that the data can never be mistaken for a fleet.
"""

from __future__ import annotations

from fleetview.demo import (
    BRAIN,
    DEMO_RUN_PREFIX,
    WORKER_CLAUDE,
    WORKER_CODEX,
    build_demo_events,
    build_quota_wall,
    demo_run_id,
)
from fleetview.projection import FleetProjection
from fleetview.schema.events import AgentState


def project(events):
    fleet = FleetProjection()
    fleet.rebuild(events)
    return fleet


def test_the_scenario_reaches_the_states_the_gate_names():
    """§7's demo: one worker working, one stalled on a human, a blocked brain."""
    events = build_demo_events(demo_run_id())
    fleet = project(events)

    assert fleet.agent(WORKER_CODEX).state is AgentState.RUNNING
    assert fleet.agent(WORKER_CLAUDE).state is AgentState.WAITING_ON_HUMAN
    assert fleet.agent(BRAIN).state is AgentState.WAITING_ON_SUBAGENT


def test_the_brain_renders_blocked_edges_with_their_dependencies():
    fleet = project(build_demo_events(demo_run_id()))
    snapshot = fleet.snapshot()

    brain = fleet.agent(BRAIN)
    assert brain.blocking_dependency_ids == [WORKER_CODEX, WORKER_CLAUDE]
    assert {e.target for e in snapshot.edges} == {WORKER_CODEX, WORKER_CLAUDE}
    assert all(e.source == BRAIN for e in snapshot.edges)


def test_the_demo_shows_cross_provider_delegation():
    """§3.1: a Claude brain and a Codex worker is the project's reason to exist."""
    fleet = project(build_demo_events(demo_run_id()))
    assert fleet.agent(BRAIN).provider == "claude"
    assert fleet.agent(WORKER_CODEX).provider == "codex"


def test_every_seeded_event_is_marked_synthetic():
    """Seeded data sharing a store with real telemetry is only safe while it
    cannot be mistaken for a fleet -- and that has to be a property of the
    data, not of the operator remembering which command they ran."""
    run_id = demo_run_id()
    events = build_demo_events(run_id) + build_quota_wall(run_id)

    assert run_id.startswith(DEMO_RUN_PREFIX)
    for event in events:
        assert event.provider_metadata["synthetic"] is True
        assert event.run_id == run_id

    for view in project(events).snapshot().agents:
        assert view.synthetic is True


def test_the_quota_wall_parks_rather_than_failing():
    """§8.5 requirement 2: park, never retry through a rate limit."""
    run_id = demo_run_id()
    events = build_demo_events(run_id)
    fleet = project(events + build_quota_wall(run_id))

    worker = fleet.agent(WORKER_CODEX)
    assert worker.state is AgentState.RATE_LIMITED
    assert worker.terminated is False, "parked, not dead"
    assert worker.state_before_rate_limit is AgentState.RUNNING


def test_the_quota_wall_lands_after_the_run_it_interrupts():
    """The default timestamp is *now*, which is earlier than the scripted
    offsets -- so the worker would show a last-seen time before its own tool
    call, and the waterfall would order it wrongly."""
    run_id = demo_run_id()
    events = build_demo_events(run_id)
    wall = build_quota_wall(run_id)
    assert wall[0].timestamp > max(e.timestamp for e in events)


def test_events_are_ordered_and_monotonic():
    events = build_demo_events(demo_run_id())
    stamps = [e.timestamp for e in events]
    assert stamps == sorted(stamps)
