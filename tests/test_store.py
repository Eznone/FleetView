"""Event store behaviour and the §4.1 storage invariants."""

import asyncio
import re
import sqlite3

import pytest

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.schema.events import EventType, FleetViewEvent
from fleetview.store import EventStore, apply_schema, connect
from fleetview.store.db import SCHEMA_PATH


@pytest.fixture
async def store(tmp_path):
    conn = await connect(tmp_path / "test.db")
    await apply_schema(conn)
    st = EventStore(conn, settings=Settings(home=tmp_path, flush_interval_ms=10))
    await st.start()
    yield st
    await st.stop()
    await conn.close()


def make_event(run_id="run-1", agent_id="agent-1", **kw):
    return FleetViewEvent(
        run_id=run_id,
        agent_id=agent_id,
        channel=kw.pop("channel", "hook"),
        event_type=kw.pop("event_type", EventType.TOOL_INVOKED),
        payload=kw.pop("payload", {}),
        **kw,
    )


# --- the tier-2 invariant ----------------------------------------------------

def test_no_blob_column_anywhere_in_the_schema():
    """§4.1 names row-per-chunk terminal bytes as *the* scaling mistake this
    schema invites: 50-500 KB/min per agent, multiple GB for a 15-agent day.
    The bytes belong in flat files with {path, offset, length} in the DB.

    A comment saying so is not enforcement. This is."""
    sql = SCHEMA_PATH.read_text()
    without_comments = re.sub(r"--[^\n]*", "", sql)
    assert "BLOB" not in without_comments.upper(), "a BLOB column reached the schema"


def test_terminal_chunks_stores_only_a_reference(tmp_path):
    """The tier-2 table must describe where bytes live, never hold them."""
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.executescript(SCHEMA_PATH.read_text())
    columns = {row[1] for row in conn.execute("PRAGMA table_info(terminal_chunks)")}
    conn.close()
    assert columns == {"id", "agent_id", "path", "byte_offset", "length", "created_at"}


def test_pragmas_are_applied(tmp_path):
    async def check():
        conn = await connect(tmp_path / "p.db")
        async with conn.execute("PRAGMA journal_mode") as cur:
            journal = (await cur.fetchone())[0]
        async with conn.execute("PRAGMA foreign_keys") as cur:
            fk = (await cur.fetchone())[0]
        await conn.close()
        return journal, fk

    journal, fk = asyncio.run(check())
    assert journal.lower() == "wal"
    assert fk == 1


# --- append and read ---------------------------------------------------------

async def test_round_trip(store):
    store.append(make_event(payload={"tool": "Edit"}))
    await store.flush()
    events = await store.fetch(run_id="run-1")
    assert len(events) == 1
    assert events[0].payload == {"tool": "Edit"}
    assert events[0].event_type is EventType.TOOL_INVOKED
    assert events[0].channel == "hook"


async def test_sequence_is_monotonic_per_run(store):
    for _ in range(10):
        store.append(make_event(run_id="run-a"))
    for _ in range(5):
        store.append(make_event(run_id="run-b"))
    await store.flush()

    a = [e.sequence for e in await store.fetch(run_id="run-a")]
    b = [e.sequence for e in await store.fetch(run_id="run-b")]
    assert a == list(range(1, 11))
    assert b == list(range(1, 6)), "each run numbers independently from 1"


async def test_sequence_resumes_from_disk(tmp_path):
    """A daemon restart must not restart numbering — replay would be ambiguous."""
    conn = await connect(tmp_path / "r.db")
    await apply_schema(conn)
    first = EventStore(conn, settings=Settings(home=tmp_path, flush_interval_ms=10))
    await first.start()
    for _ in range(3):
        first.append(make_event(run_id="run-x"))
    await first.stop()

    second = EventStore(conn, settings=Settings(home=tmp_path, flush_interval_ms=10))
    await second.start()
    second.append(make_event(run_id="run-x"))
    await second.flush()
    await second.stop()

    assert [e.sequence for e in await second.fetch(run_id="run-x")] == [1, 2, 3, 4]
    await conn.close()


async def test_batch_commits_in_order_within_the_flush_window(store):
    """Everything queued inside one window lands in one transaction, ordered as
    it was appended."""
    for i in range(200):
        store.append(make_event(payload={"i": i}))
    await store.flush()

    events = await store.fetch(run_id="run-1", limit=500)
    assert len(events) == 200
    assert [e.payload["i"] for e in events] == list(range(200))


async def test_stop_drains_rather_than_dropping(tmp_path):
    conn = await connect(tmp_path / "d.db")
    await apply_schema(conn)
    st = EventStore(conn, settings=Settings(home=tmp_path, flush_interval_ms=50))
    await st.start()
    for _ in range(50):
        st.append(make_event())
    await st.stop()
    assert await st.count() == 50
    await conn.close()


async def test_filters(store):
    store.append(make_event(agent_id="a", event_type=EventType.TOOL_INVOKED))
    store.append(make_event(agent_id="b", event_type=EventType.QUOTA_EXHAUSTED))
    store.append(make_event(agent_id="b", payload={"tool": "Bash"}))
    await store.flush()

    assert len(await store.fetch(agent_id="b")) == 2
    assert len(await store.fetch(event_type="quota.exhausted")) == 1
    assert len(await store.fetch(grep="Bash")) == 1
    assert len(await store.fetch(grep="quota")) == 1, "grep matches the event type too"


async def test_after_sequence_supports_tailing(store):
    for _ in range(5):
        store.append(make_event())
    await store.flush()
    assert [e.sequence for e in await store.fetch(after_sequence=3)] == [4, 5]


# --- bus integration ---------------------------------------------------------

async def test_events_reach_the_bus_only_after_commit(tmp_path):
    """A subscriber must never see an event the log would lose on a crash."""
    conn = await connect(tmp_path / "b.db")
    await apply_schema(conn)
    bus = EventBus()
    sub = bus.subscribe("events")
    st = EventStore(conn, bus=bus, settings=Settings(home=tmp_path, flush_interval_ms=10))
    await st.start()

    st.append(make_event(agent_id="agent-7"))
    assert sub.queue.empty(), "published before the commit"

    await st.flush()
    topic, event = sub.queue.get_nowait()
    assert topic == "events.agent-7"
    assert event.sequence == 1, "the sequence is assigned by the time it is published"

    await st.stop()
    await conn.close()


async def test_newest_returns_the_last_n_in_order(store):
    """`events tail --limit 3` must mean the three most recent, still oldest-
    first on stdout — not the first three of the run."""
    for i in range(10):
        store.append(make_event(payload={"i": i}))
    await store.flush()

    events = await store.fetch(run_id="run-1", limit=3, newest=True)
    assert [e.payload["i"] for e in events] == [7, 8, 9]
