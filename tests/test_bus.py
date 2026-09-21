"""Bus routing, and the rule that a slow subscriber cannot stall ingest."""

from fleetview.bus import EventBus


def test_prefix_subscription_is_delimiter_aware():
    """Subscribing to agent.claude must not also deliver agent.claude_2's
    traffic — a bare startswith() gets this wrong, and the symptom is one
    agent's pane showing another's events."""
    bus = EventBus()
    sub = bus.subscribe("agent.claude")
    bus.publish("agent.claude", 1)
    bus.publish("agent.claude.tool", 2)
    bus.publish("agent.claude_2", 3)
    bus.publish("agent.codex", 4)

    assert [bus_event for _, bus_event in _drain(sub)] == [1, 2]


def test_empty_prefix_receives_everything():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish("anything", "a")
    bus.publish("other.topic", "b")
    assert len(_drain(sub)) == 2


def test_subscribers_are_independent():
    bus = EventBus()
    a, b = bus.subscribe("x"), bus.subscribe("x")
    bus.publish("x", 1)
    assert len(_drain(a)) == 1
    assert len(_drain(b)) == 1


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    sub = bus.subscribe("x")
    bus.unsubscribe(sub)
    bus.publish("x", 1)
    assert sub.queue.empty()
    assert bus.subscriber_count == 0


def test_a_full_subscriber_drops_oldest_instead_of_blocking():
    """The publisher is the ingest path, which may be holding a worker's tool
    call open. It must never wait on a UI that stopped reading. The durable
    copy is in SQLite regardless, so dropping here is safe — but it is counted,
    so the gap is visible rather than silent."""
    bus = EventBus()
    sub = bus.subscribe("x", maxsize=3)
    for i in range(6):
        bus.publish("x", i)

    assert [event for _, event in _drain(sub)] == [3, 4, 5], "newest kept"
    assert sub.dropped == 3
    assert bus.dropped_total == 3


def test_publish_with_no_subscribers_is_a_no_op():
    EventBus().publish("x", 1)


def _drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out
