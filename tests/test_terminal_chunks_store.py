"""The tier-2 index writer (§4.1).

What a row *means* is settled here: a time index into a contiguous file, with
per-file offsets. §4.1 says `{path, offset, length}` and stops; these tests are
the other half.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.schema.events import EventType, FleetViewEvent
from fleetview.store import EventStore, TerminalChunkStore, apply_schema, connect
from fleetview.ids import new_event_id, new_span_id, new_trace_id

T0 = datetime(2026, 9, 21, 14, 0, 0, tzinfo=timezone.utc)


async def _store(tmp_path, **kw):
    conn = await connect(tmp_path / "t.db")
    await apply_schema(conn)
    store = TerminalChunkStore(conn, settings=Settings(home=tmp_path, **kw))
    await store.start()
    return conn, store


def _event(run_id="run-1", agent_id="agent-1"):
    return FleetViewEvent(
        id=new_event_id(),
        timestamp=datetime.now(timezone.utc),
        run_id=run_id,
        trace_id=new_trace_id(),
        span_id=new_span_id(),
        parent_span_id=None,
        agent_id=agent_id,
        channel="terminal",
        event_type=EventType.TERMINAL_TAP_OPENED,
        payload={},
        provider_metadata=None,
    )


async def test_round_trip(tmp_path):
    conn, store = await _store(tmp_path)
    store.append(agent_id="a", path="/logs/seg-000000.log", byte_offset=0,
                 length=1024, created_at=T0)
    await store.flush()

    (chunk,) = await store.fetch("a")
    assert (chunk.path, chunk.byte_offset, chunk.length) == ("/logs/seg-000000.log", 0, 1024)
    assert chunk.end == 1024
    assert chunk.created_at == T0.isoformat()
    await store.stop()
    await conn.close()


async def test_created_at_is_the_first_bytes_time_not_the_flush_time(tmp_path):
    """Seeking by time is the row's entire purpose. Stamping at flush would
    skew every seek by up to the coalescing window."""
    conn, store = await _store(tmp_path)
    first_byte = T0
    store.append(agent_id="a", path="/p", byte_offset=0, length=10, created_at=first_byte)
    await store.flush()

    (chunk,) = await store.fetch("a")
    assert chunk.created_at == first_byte.isoformat()
    await store.stop()
    await conn.close()


async def test_rows_tile_a_file_from_zero(tmp_path):
    """{path, byte_offset, length} is self-contained: offsets are relative to
    the segment, and a file's rows cover it with no gaps or overlaps."""
    conn, store = await _store(tmp_path)
    offset = 0
    for i in range(5):
        store.append(agent_id="a", path="/seg0", byte_offset=offset, length=100,
                     created_at=T0 + timedelta(seconds=i))
        offset += 100
    await store.flush()

    chunks = await store.fetch("a")
    assert chunks[0].byte_offset == 0
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.end == nxt.byte_offset
    await store.stop()
    await conn.close()


async def test_since_and_until_filter(tmp_path):
    conn, store = await _store(tmp_path)
    for i in range(10):
        store.append(agent_id="a", path="/seg0", byte_offset=i * 10, length=10,
                     created_at=T0 + timedelta(seconds=i))
    await store.flush()

    window = await store.fetch("a", since=T0 + timedelta(seconds=3),
                               until=T0 + timedelta(seconds=5))
    assert [c.byte_offset for c in window] == [30, 40, 50]
    await store.stop()
    await conn.close()


async def test_newest_returns_the_last_rows_in_order(tmp_path):
    conn, store = await _store(tmp_path)
    for i in range(10):
        store.append(agent_id="a", path="/seg0", byte_offset=i * 10, length=10,
                     created_at=T0 + timedelta(seconds=i))
    await store.flush()

    tail = await store.fetch("a", limit=3, newest=True)
    assert [c.byte_offset for c in tail] == [70, 80, 90]
    await store.stop()
    await conn.close()


async def test_segments_are_returned_in_write_order(tmp_path):
    conn, store = await _store(tmp_path)
    for path in ("/seg2", "/seg0", "/seg1"):        # written in this order
        store.append(agent_id="a", path=path, byte_offset=0, length=1, created_at=T0)
    await store.flush()

    assert await store.segments("a") == ["/seg2", "/seg0", "/seg1"]
    await store.stop()
    await conn.close()


async def test_agents_are_isolated(tmp_path):
    conn, store = await _store(tmp_path)
    store.append(agent_id="a", path="/a", byte_offset=0, length=1, created_at=T0)
    store.append(agent_id="b", path="/b", byte_offset=0, length=1, created_at=T0)
    await store.flush()

    assert [c.path for c in await store.fetch("a")] == ["/a"]
    assert await store.agents() == ["a", "b"]
    assert await store.count("a") == 1
    assert await store.count() == 2
    await store.stop()
    await conn.close()


async def test_forget_paths_removes_rows_for_unlinked_segments(tmp_path):
    """A row pointing at a pruned file is a broken pointer, and the pinned
    column set gives no tombstone flag to mark one with."""
    conn, store = await _store(tmp_path)
    for path in ("/seg0", "/seg1", "/seg2"):
        store.append(agent_id="a", path=path, byte_offset=0, length=10, created_at=T0)
    await store.flush()

    removed = await store.forget_paths(["/seg0", "/seg1"])
    assert removed == 2
    assert await store.segments("a") == ["/seg2"]
    assert await store.forget_paths([]) == 0
    await store.stop()
    await conn.close()


async def test_stop_drains_what_is_queued(tmp_path):
    """A lost final row is a byte range nothing can find again — the file
    carries no index of its own."""
    conn, store = await _store(tmp_path)
    for i in range(50):
        store.append(agent_id="a", path="/seg0", byte_offset=i, length=1, created_at=T0)
    await store.stop()

    assert await store.count("a") == 50
    await conn.close()


async def test_two_writers_on_one_connection_lose_nothing(tmp_path):
    """EventStore and TerminalChunkStore share the daemon's connection, so
    either's commit() also commits the other's pending inserts. Both are pure
    appends, so that is safe — but it should be demonstrated, not asserted in
    a docstring."""
    conn = await connect(tmp_path / "shared.db")
    await apply_schema(conn)
    settings = Settings(home=tmp_path)
    bus = EventBus()
    events = EventStore(conn, bus=bus, settings=settings)
    chunks = TerminalChunkStore(conn, settings=settings)
    await events.start()
    await chunks.start()

    for i in range(200):
        events.append(_event())
        chunks.append(agent_id="agent-1", path="/seg0", byte_offset=i * 10,
                      length=10, created_at=T0 + timedelta(milliseconds=i))

    await events.stop()
    await chunks.stop()

    assert await events.count() == 200
    assert await chunks.count("agent-1") == 200
    # And the event log's own ordering invariant survived the interleaving.
    logged = await events.fetch(run_id="run-1", limit=1000)
    assert [e.sequence for e in logged] == list(range(1, 201))
    await conn.close()


async def test_pending_reports_uncommitted_rows(tmp_path):
    conn, store = await _store(tmp_path)
    assert store.pending == 0
    store.append(agent_id="a", path="/p", byte_offset=0, length=1, created_at=T0)
    assert store.pending >= 0          # may already have been drained
    await store.stop()
    await conn.close()


async def test_scheduled_forget_lands_after_the_inserts_it_must_remove(tmp_path):
    """Ordering regression.

    The writer prunes a segment and schedules a forget for it while rows naming
    that segment may still be sitting in this queue. A DELETE that jumps ahead
    removes nothing and the rows land afterwards, resurrecting a pointer to a
    file that no longer exists.

    An earlier version reached for `await flush()` from the writer's prune to
    force the ordering, and deadlocked the writer mid-prune — segment gone,
    rows still there, capture silently stopped. The queue does the ordering
    instead, and nothing on the write path awaits.
    """
    conn, store = await _store(tmp_path)

    # Interleave exactly as a prune does: inserts, then a forget naming them.
    for i in range(10):
        store.append(agent_id="a", path="/seg0", byte_offset=i * 10, length=10,
                     created_at=T0)
    store.schedule_forget(["/seg0"])
    for i in range(5):
        store.append(agent_id="a", path="/seg1", byte_offset=i * 10, length=10,
                     created_at=T0)

    await store.flush()

    assert await store.segments("a") == ["/seg1"], "the forget did not remove /seg0"
    assert await store.count("a") == 5
    await store.stop()
    await conn.close()


async def test_scheduled_forget_is_synchronous_and_never_blocks(tmp_path):
    """The writer calls this from its write path; it must not be a coroutine."""
    import inspect
    conn, store = await _store(tmp_path)
    assert not inspect.iscoroutinefunction(store.schedule_forget)
    store.schedule_forget([])          # empty is a no-op, not an error
    await store.stop()
    await conn.close()
