"""`fleetview terminal`.

Built like `events tail`: reads files and SQLite directly, no daemon. That is
the point — the post-mortem case is exactly when the daemon is not running.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from fleetview.bus import EventBus
from fleetview.cli import app
from fleetview.config import Settings
from fleetview.store import TerminalChunkStore, apply_schema, connect
from fleetview.terminal.writer import LogWriter, output_topic

runner = CliRunner()
AGENT = "agent-1"


async def _capture(tmp_path, *buffers: bytes, **overrides):
    """Lay down a real capture the CLI can then read back."""
    settings = Settings(home=tmp_path, **overrides)
    settings.ensure_directories()
    conn = await connect(settings.db_path)
    await apply_schema(conn)
    chunks = TerminalChunkStore(conn, settings=settings)
    await chunks.start()
    bus = EventBus()
    writer = LogWriter(AGENT, settings=settings, chunks=chunks, bus=bus)
    await writer.start()
    for buf in buffers:
        bus.publish(output_topic(AGENT), buf)
        while not writer._sub.queue.empty():
            _t, data = writer._sub.queue.get_nowait()
            await writer._handle(data)
    await writer.stop()
    await chunks.stop()
    await conn.close()
    return settings


def _run(tmp_path, *args):
    return runner.invoke(app, list(args), env={"FLEETVIEW_HOME": str(tmp_path)})


def test_tail_emits_the_exact_bytes(tmp_path):
    asyncio.run(_capture(tmp_path, b"hello ", b"from the pane"))
    result = _run(tmp_path, "terminal", "tail", AGENT)
    assert result.exit_code == 0
    assert "hello from the pane" in result.stdout


def test_tail_preserves_ansi_by_default(tmp_path):
    asyncio.run(_capture(tmp_path, b"\x1b[31mred\x1b[0m"))
    result = _run(tmp_path, "terminal", "tail", AGENT)
    assert "\x1b[31m" in result.stdout


def test_strip_ansi_makes_it_greppable(tmp_path):
    asyncio.run(_capture(tmp_path, b"\x1b[31mred\x1b[0m plain"))
    result = _run(tmp_path, "terminal", "tail", AGENT, "--strip-ansi")
    assert "\x1b[" not in result.stdout
    assert "red plain" in result.stdout


def test_bytes_limits_the_history_shown(tmp_path):
    asyncio.run(_capture(tmp_path, b"A" * 1000 + b"TAIL"))
    result = _run(tmp_path, "terminal", "tail", AGENT, "--bytes", "4")
    assert result.stdout.strip() == "TAIL"


def test_tail_reads_across_a_rotation(tmp_path):
    """A capture that rotated must still read back as one continuous stream."""
    asyncio.run(_capture(
        tmp_path, *[b"x" * 4096 for _ in range(8)],
        terminal_segment_max_bytes=8 * 1024,
    ))
    result = _run(tmp_path, "terminal", "tail", AGENT, "--bytes", "40000")
    assert result.stdout.count("x") == 8 * 4096


def test_since_seeks_by_time(tmp_path):
    settings = asyncio.run(_capture(tmp_path, b"early bytes"))

    async def add_later():
        conn = await connect(settings.db_path)
        chunks = TerminalChunkStore(conn, settings=settings)
        rows = await chunks.fetch(AGENT)
        await conn.close()
        return rows

    rows = asyncio.run(add_later())
    assert rows
    before = (datetime.fromisoformat(rows[0].created_at) - timedelta(minutes=1)).isoformat()
    after = (datetime.fromisoformat(rows[0].created_at) + timedelta(minutes=1)).isoformat()

    assert "early bytes" in _run(tmp_path, "terminal", "tail", AGENT, "--since", before).stdout
    missed = _run(tmp_path, "terminal", "tail", AGENT, "--since", after)
    assert missed.exit_code == 1


def test_tail_on_an_unknown_agent_exits_cleanly(tmp_path):
    asyncio.run(_capture(tmp_path, b"something"))
    result = _run(tmp_path, "terminal", "tail", "nobody")
    assert result.exit_code == 1
    assert "no terminal capture" in result.output


def test_an_unsafe_agent_id_is_refused_not_resolved(tmp_path):
    asyncio.run(_capture(tmp_path, b"x"))
    result = _run(tmp_path, "terminal", "tail", "../../etc/passwd")
    assert result.exit_code == 1


def test_tail_without_a_store_says_so(tmp_path):
    result = _run(tmp_path, "terminal", "tail", AGENT)
    assert result.exit_code == 1
    assert "fleetview init" in result.output


def test_ls_reports_what_was_captured(tmp_path):
    asyncio.run(_capture(tmp_path, b"z" * 5000))
    result = _run(tmp_path, "terminal", "ls")
    assert result.exit_code == 0
    assert AGENT in result.stdout
    assert "5,000" in result.stdout
    assert "segments 1" in result.stdout


def test_ls_on_an_empty_store_exits_cleanly(tmp_path):
    settings = Settings(home=tmp_path)
    settings.ensure_directories()

    async def make():
        conn = await connect(settings.db_path)
        await apply_schema(conn)
        await conn.close()

    asyncio.run(make())
    result = _run(tmp_path, "terminal", "ls")
    assert result.exit_code == 0
    assert "nothing captured yet" in result.stdout


def test_selftest_passes(tmp_path):
    """The diagnostic the manual procedure starts with."""
    result = _run(tmp_path, "terminal", "selftest")
    assert result.exit_code == 0
    assert "PASS" in result.stdout


def test_attach_without_a_daemon_names_the_problem(tmp_path):
    result = _run(tmp_path, "terminal", "attach", AGENT, "--session", "s")
    assert result.exit_code == 1
    assert "no daemon" in result.output


def test_init_reports_the_directory_modes(tmp_path):
    """Printed so the operator can see the mitigation rather than assume it."""
    result = _run(tmp_path, "init")
    assert result.exit_code == 0
    assert "0o700" in result.stdout
    assert result.stdout.count("0o700") == 3
