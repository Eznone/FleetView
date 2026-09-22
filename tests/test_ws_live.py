"""`/ws/live` -- the feed the canvas renders from.

Two things are being guarded here. The §7 gate ("node state updates <100 ms")
is one. The other is subscription hygiene: `Subscription.close()` only sets a
flag and does not wake a task blocked in `queue.get()`, so a handler that
unsubscribes without cancelling leaks a pump forever, and every leaked
subscription is permanent work on the ingest fan-out.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import partial

from starlette.testclient import TestClient

from fleetview.schema.events import AgentState, EventType, FleetViewEvent
from uiharness import BASE_URL, RUN, build, emit, ws


def event(event_type, *, agent="a1", payload=None) -> FleetViewEvent:
    return FleetViewEvent(
        run_id=RUN, agent_id=agent, channel="hook",
        event_type=event_type, payload=payload or {},
        timestamp=datetime.now(timezone.utc),
    )


def push(client, hook, item) -> None:
    client.portal.call(partial(emit, hook.state.store, item))


def read_until(socket, wanted: str, *, limit: int = 12) -> dict:
    """Pull frames until one of ``wanted`` type arrives."""
    for _ in range(limit):
        frame = socket.receive_json()
        if frame["type"] == wanted:
            return frame
    raise AssertionError(f"no {wanted!r} frame in {limit} messages")


def test_a_client_is_handed_the_whole_fleet_on_connect(tmp_path):
    """The canvas must be able to draw itself from cold, with no replay."""
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        push(client, hook, event(EventType.AGENT_READY))
        with ws(client, "/ws/live") as socket:
            first = socket.receive_json()
            assert first["type"] == "snapshot"
            assert [a["agentId"] for a in first["snapshot"]["agents"]] == ["a1"]


def test_a_state_change_reaches_the_client(tmp_path):
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/live") as socket:
            assert socket.receive_json()["type"] == "snapshot"
            push(client, hook, event(EventType.TASK_STARTED, payload={"prompt": "go"}))

            frame = read_until(socket, "snapshot")
            agent = frame["snapshot"]["agents"][0]
            assert agent["agentId"] == "a1"
            assert agent["state"] == AgentState.RUNNING


def test_the_raw_event_feed_reaches_the_client_too(tmp_path):
    """§5.6's waterfall and the inspector's raw-payload drawer read this."""
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/live") as socket:
            socket.receive_json()
            push(client, hook, event(
                EventType.TOOL_REQUESTED, payload={"tool_name": "Bash"}
            ))
            frame = read_until(socket, "event")

    assert frame["event"]["eventType"] == EventType.TOOL_REQUESTED
    assert frame["event"]["agentId"] == "a1", "the envelope is camelCase"
    assert frame["event"]["payload"]["tool_name"] == "Bash", "payload stays snake_case"


def test_node_state_updates_within_100ms(tmp_path):
    """§7's Phase 2 gate.

    The budget is the group-commit window (50 ms, `flush_interval_ms`) plus the
    post-commit fan-out plus one socket write. This harness flushes explicitly,
    so what is measured is the fold and the fan-out -- which is the part Phase 2
    adds and therefore the part that can regress.
    """
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/live") as socket:
            socket.receive_json()

            started = time.perf_counter()
            push(client, hook, event(EventType.TASK_STARTED, payload={"prompt": "go"}))
            frame = read_until(socket, "snapshot")
            elapsed_ms = (time.perf_counter() - started) * 1000

    assert frame["snapshot"]["agents"][0]["state"] == AgentState.RUNNING
    assert elapsed_ms < 100, f"node state took {elapsed_ms:.1f} ms"


def test_a_disconnected_client_releases_its_subscription(tmp_path):
    """A leak here is permanent work on the ingest hot path.

    The bus fans out to every subscriber on `publish`, which the store calls
    inside the commit path -- so a subscription nobody reads is not merely
    idle, it is a queue that fills and then drops on every single event for the
    life of the daemon.
    """
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        bus = hook.state.bus
        before = bus.subscriber_count

        with ws(client, "/ws/live") as socket:
            socket.receive_json()
            assert bus.subscriber_count == before + 2, "one for events, one for the fleet"

        deadline = time.monotonic() + 2.0
        while bus.subscriber_count != before and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bus.subscriber_count == before
        assert ui.state.clients.count == 0


def test_two_clients_each_get_their_own_feed(tmp_path):
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/live") as one, \
             ws(client, "/ws/live") as two:
            one.receive_json()
            two.receive_json()
            push(client, hook, event(EventType.TASK_STARTED, payload={"prompt": "go"}))

            for socket in (one, two):
                frame = read_until(socket, "snapshot")
                assert frame["snapshot"]["agents"][0]["state"] == AgentState.RUNNING


def test_blocked_edges_arrive_over_the_socket(tmp_path):
    """§7's gate again, this time on the transport the canvas actually uses."""
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/live") as socket:
            socket.receive_json()
            push(client, hook, event(
                EventType.MESSAGE_SENT, agent="brain",
                payload={"to": "codex-1", "orchestration": "handoff"},
            ))
            frame = read_until(socket, "snapshot")

    snapshot = frame["snapshot"]
    brain = next(a for a in snapshot["agents"] if a["agentId"] == "brain")
    assert brain["state"] == AgentState.WAITING_ON_SUBAGENT
    assert brain["blockingDependencyIds"] == ["codex-1"]
    assert snapshot["edges"][0]["source"] == "brain"
