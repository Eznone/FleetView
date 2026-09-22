"""The supervisor: idempotent attach, discovery-based reconcile, ordered stop."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.schema.events import EventType
from fleetview.spawn.tmux import TerminalTapError
from fleetview.store import EventStore, TerminalChunkStore, apply_schema, connect
from fleetview.terminal.supervisor import NullTerminalSupervisor, TerminalSupervisor


class FakeResult:
    def __init__(self, stdout): self.stdout = stdout


class FakePane:
    def __init__(self): self.piped = "0"; self.calls = []
    def cmd(self, *args):
        self.calls.append(args)
        if args[0] == "display-message":
            return FakeResult([self.piped])
        if args[0] == "pipe-pane":
            self.piped = "0" if len(args) == 1 else "1"
        return FakeResult([])


class FakeWindow:
    def __init__(self, pane): self.active_pane = pane


class FakeSession:
    def __init__(self, name, agent_id):
        self.name = name
        self.agent_id = agent_id
        self.pane = FakePane()
        self.active_window = FakeWindow(self.pane)
    def cmd(self, *args):
        if args[0] == "show-environment":
            return FakeResult([f"FLEETVIEW_AGENT_ID={self.agent_id}"])
        return FakeResult([])


class FakeServer:
    def __init__(self, *sessions): self.sessions = list(sessions)


class Rig:
    def __init__(self, conn, events, chunks, bus, sup, server, settings):
        self.conn, self.events, self.chunks = conn, events, chunks
        self.bus, self.sup, self.server, self.settings = bus, sup, server, settings

    async def close(self):
        await self.sup.stop()
        await self.events.stop()
        await self.chunks.stop()
        await self.conn.close()

    async def event_types(self):
        await self.events.flush()
        return [e.event_type for e in await self.events.fetch(limit=500)]


async def rig(tmp_path, *sessions, **overrides) -> Rig:
    settings = Settings(home=tmp_path, terminal_reconcile_seconds=3600, **overrides)
    settings.ensure_directories()
    conn = await connect(settings.db_path)
    await apply_schema(conn)
    bus = EventBus()
    events = EventStore(conn, bus=bus, settings=settings)
    await events.start()
    chunks = TerminalChunkStore(conn, settings=settings)
    await chunks.start()
    server = FakeServer(*sessions)
    sup = TerminalSupervisor(settings, chunks=chunks, events=events, bus=bus,
                             run_id="run-1", server=server)
    return Rig(conn, events, chunks, bus, sup, server, settings)


# --- attaching ---------------------------------------------------------------

async def test_attach_taps_the_pane_and_creates_the_fifo(tmp_path):
    session = FakeSession("sess-1", "agent-1")
    r = await rig(tmp_path, session)
    handle = await r.sup.attach("agent-1", "sess-1")

    assert session.pane.piped == "1"
    assert handle.fifo.exists()
    assert r.sup.count == 1
    assert EventType.TERMINAL_TAP_OPENED in await r.event_types()
    await r.close()


async def test_attach_is_idempotent(tmp_path):
    """A second attach must not be the thing that switches capture off — the
    `-o` trap, one level up."""
    session = FakeSession("sess-1", "agent-1")
    r = await rig(tmp_path, session)
    first = await r.sup.attach("agent-1", "sess-1")
    second = await r.sup.attach("agent-1", "sess-1")

    assert first is second
    assert session.pane.piped == "1"
    assert r.sup.count == 1
    await r.close()


async def test_attaching_to_a_missing_session_is_an_error(tmp_path):
    r = await rig(tmp_path)
    with pytest.raises(TerminalTapError, match="no tmux session"):
        await r.sup.attach("agent-1", "nope")
    assert r.sup.count == 0
    await r.close()


async def test_a_tap_that_will_not_verify_cleans_up_after_itself(tmp_path):
    """No half-attached state: no orphan FIFO, no orphan writer task."""
    class NeverPipes(FakeSession):
        def __init__(self, *a):
            super().__init__(*a)
            self.pane.cmd = lambda *args: FakeResult(["0"])

    session = NeverPipes("sess-1", "agent-1")
    r = await rig(tmp_path, session)
    with pytest.raises(TerminalTapError):
        await r.sup.attach("agent-1", "sess-1")

    assert r.sup.count == 0
    from fleetview.terminal.paths import fifo_path
    assert not fifo_path(r.settings, "agent-1").exists()
    assert EventType.TERMINAL_TAP_FAILED in await r.event_types()
    await r.close()


# --- bytes end to end --------------------------------------------------------

async def test_bytes_written_to_the_fifo_reach_the_log_and_the_index(tmp_path):
    """The whole plane, minus tmux: FIFO -> reader -> bus -> writer -> disk."""
    session = FakeSession("sess-1", "agent-1")
    r = await rig(tmp_path, session, terminal_chunk_idle_ms=0)
    handle = await r.sup.attach("agent-1", "sess-1")

    subprocess.run(["sh", "-c", f"printf 'pane output here' >> {handle.fifo}"],
                   check=True, timeout=10)
    for _ in range(200):
        await asyncio.sleep(0)

    await r.sup.detach("agent-1")
    await r.chunks.flush()

    from fleetview.terminal.paths import agent_log_dir, list_segments
    segments = list_segments(agent_log_dir(r.settings, "agent-1"))
    assert b"".join(p.read_bytes() for p in segments) == b"pane output here"

    rows = await r.chunks.fetch("agent-1")
    assert rows and rows[-1].length == len(b"pane output here")
    await r.close()


# --- reconcile ---------------------------------------------------------------

async def test_reconcile_discovers_an_untapped_agent(tmp_path):
    """tmux is the registry: the daemon asks rather than remembers."""
    r = await rig(tmp_path, FakeSession("sess-1", "agent-1"))
    assert r.sup.count == 0

    live = await r.sup.reconcile()

    assert live == {"agent-1": "sess-1"}
    assert r.sup.count == 1
    await r.close()


async def test_reconcile_tears_down_a_vanished_session(tmp_path):
    session = FakeSession("sess-1", "agent-1")
    r = await rig(tmp_path, session)
    handle = await r.sup.attach("agent-1", "sess-1")
    assert handle.fifo.exists()

    r.server.sessions = []          # the agent exited
    await r.sup.reconcile()

    assert r.sup.count == 0
    assert not handle.fifo.exists()
    assert EventType.TERMINAL_TAP_CLOSED in await r.event_types()
    await r.close()


async def test_reconcile_ignores_sessions_that_are_not_ours(tmp_path):
    class Unmarked(FakeSession):
        def cmd(self, *args):
            if args[0] == "show-environment":
                return FakeResult(["-FLEETVIEW_AGENT_ID"])
            return FakeResult([])

    r = await rig(tmp_path, Unmarked("someones-shell", None))
    assert await r.sup.reconcile() == {}
    assert r.sup.count == 0
    await r.close()


async def test_reconcile_is_how_a_restart_recovers(tmp_path):
    """A second supervisor over the same live session re-attaches, which is
    what makes a daemon restart self-healing."""
    session = FakeSession("sess-1", "agent-1")
    r = await rig(tmp_path, session)
    await r.sup.attach("agent-1", "sess-1")
    await r.sup.stop()                       # daemon goes down

    sup2 = TerminalSupervisor(r.settings, chunks=r.chunks, events=r.events,
                              bus=r.bus, run_id="run-2", server=r.server)
    await sup2.reconcile()
    assert sup2.count == 1
    assert session.pane.piped == "1"
    await sup2.stop()
    await r.events.stop(); await r.chunks.stop(); await r.conn.close()


# --- shutdown ----------------------------------------------------------------

async def test_stop_indexes_the_final_bytes_and_says_it_closed(tmp_path):
    session = FakeSession("sess-1", "agent-1")
    r = await rig(tmp_path, session)
    handle = await r.sup.attach("agent-1", "sess-1")
    subprocess.run(["sh", "-c", f"printf 'last words' >> {handle.fifo}"],
                   check=True, timeout=10)
    for _ in range(200):
        await asyncio.sleep(0)

    await r.sup.stop()
    await r.chunks.flush()

    rows = await r.chunks.fetch("agent-1")
    assert rows and rows[-1].length == len(b"last words")
    assert EventType.TERMINAL_TAP_CLOSED in await r.event_types()
    assert session.pane.piped == "0", "the pipe was left attached"
    await r.events.stop(); await r.chunks.stop(); await r.conn.close()


async def test_detach_reports_whether_there_was_anything_to_detach(tmp_path):
    r = await rig(tmp_path, FakeSession("sess-1", "agent-1"))
    assert await r.sup.detach("agent-1") is False
    await r.sup.attach("agent-1", "sess-1")
    assert await r.sup.detach("agent-1") is True
    await r.close()


# --- disabled ----------------------------------------------------------------

async def test_the_null_supervisor_is_inert():
    null = NullTerminalSupervisor()
    await null.start()
    assert null.count == 0
    assert null.status() == []
    assert await null.reconcile() == {}
    assert await null.detach("a") is False
    with pytest.raises(TerminalTapError, match="disabled"):
        await null.attach("a", "s")
    await null.stop()


async def test_status_reports_what_the_ui_needs(tmp_path):
    r = await rig(tmp_path, FakeSession("sess-1", "agent-1"))
    await r.sup.attach("agent-1", "sess-1")
    (row,) = r.sup.status()
    assert row["agentId"] == "agent-1"
    assert row["tmuxSession"] == "sess-1"
    # camelCase throughout: this is an HTTP response, and the UI reads it.
    # `writer.stats` is snake_case at source because it also lands in event
    # payloads, which are not aliased -- `status()` converts at the boundary.
    assert "bytesRead" in row and "bytesWritten" in row and "truncating" in row
    assert not any("_" in key for key in row), f"snake_case leaked: {sorted(row)}"
    await r.close()
