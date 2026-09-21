"""Phase 1's tier-2 gate: terminal bytes are provably absent from the database.

An invariant, not a benchmark, so this is never behind a marker.

Deliberately byte-level rather than column-level. A future `payload` field that
helpfully embedded "the last 200 chars of output" would pass any check that
looks at the schema, and fail this one. Together with
`test_store.py::test_no_blob_column_anywhere_in_the_schema` and
`::test_terminal_chunks_stores_only_a_reference`, that is three independent
statements of the same rule, two structural and one empirical.
"""

from __future__ import annotations

import asyncio
import secrets
import subprocess

import pytest

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.store import TerminalChunkStore, apply_schema, connect
from fleetview.terminal.fifo import FifoReader
from fleetview.terminal.paths import agent_log_dir, list_segments
from fleetview.terminal.writer import LogWriter, output_topic

AGENT = "agent-secrets"
MEGABYTE = 1024 * 1024


async def test_pane_bytes_never_reach_the_database(tmp_path):
    settings = Settings(
        home=tmp_path,
        terminal_segment_max_bytes=2 * MEGABYTE,
        terminal_max_bytes_per_agent=64 * MEGABYTE,
    )
    settings.ensure_directories()
    conn = await connect(settings.db_path)
    await apply_schema(conn)
    chunks = TerminalChunkStore(conn, settings=settings)
    await chunks.start()
    bus = EventBus()
    writer = LogWriter(AGENT, settings=settings, chunks=chunks, bus=bus)
    await writer.start()

    # Something that could not plausibly occur by chance, standing in for the
    # thing this rule actually protects: a token an agent echoed into its pane.
    sentinel = secrets.token_hex(32).encode()
    db_size_before = settings.db_path.stat().st_size

    payload = b"x" * 4096
    for i in range(1280):                     # 5 MiB
        bus.publish(output_topic(AGENT), sentinel if i == 640 else payload)
        if i % 64 == 0:
            await asyncio.sleep(0)
    for _ in range(500):
        if writer._sub.queue.empty():
            break
        await asyncio.sleep(0)

    await writer.stop()
    await chunks.stop()

    # 1. The sentinel IS on disk. Without this the rest proves nothing —
    #    a capture that dropped everything would pass every assertion below.
    segments = list_segments(agent_log_dir(settings, AGENT))
    assert segments, "nothing was captured"
    assert any(sentinel in p.read_bytes() for p in segments), \
        "the sentinel never reached a segment file"

    # 2. Checkpoint so nothing is hiding in the WAL, then read the raw files.
    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await conn.commit()
    await conn.close()

    for suffix in ("", "-wal", "-shm"):
        candidate = settings.db_path.with_name(settings.db_path.name + suffix)
        if not candidate.exists():
            continue
        assert sentinel not in candidate.read_bytes(), \
            f"terminal bytes leaked into {candidate.name}"

    # 3. The database barely moved while 5 MiB landed on disk.
    growth = settings.db_path.stat().st_size - db_size_before
    assert growth < 64 * 1024, f"the database grew {growth} bytes for 5 MiB of pane output"

    # 4. And the index did not grow at the byte rate either.
    conn2 = await connect(settings.db_path)
    store2 = TerminalChunkStore(conn2, settings=settings)
    assert await store2.count(AGENT) < 100
    total_indexed = sum(c.length for c in await store2.fetch(AGENT, limit=10_000))
    assert total_indexed == writer.bytes_written, "the index disagrees with the log"
    await conn2.close()


async def test_the_gate_holds_through_a_real_fifo(tmp_path):
    """The same rule, with the bytes arriving the way they really do."""
    settings = Settings(home=tmp_path)
    settings.ensure_directories()
    conn = await connect(settings.db_path)
    await apply_schema(conn)
    chunks = TerminalChunkStore(conn, settings=settings)
    await chunks.start()
    bus = EventBus()
    writer = LogWriter(AGENT, settings=settings, chunks=chunks, bus=bus)
    await writer.start()

    sentinel = secrets.token_hex(32)
    fifo = FifoReader(settings.fifo_dir / f"{AGENT}.fifo",
                      on_bytes=lambda d: bus.publish(output_topic(AGENT), d))
    fifo.start()
    try:
        subprocess.run(
            ["sh", "-c", f"printf 'sk-live-{sentinel}\\n' >> {fifo.path}"],
            check=True, timeout=10,
        )
        for _ in range(300):
            await asyncio.sleep(0)
    finally:
        fifo.stop()

    await writer.stop()
    await chunks.stop()

    segments = list_segments(agent_log_dir(settings, AGENT))
    assert any(sentinel.encode() in p.read_bytes() for p in segments)

    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await conn.commit()
    await conn.close()
    for suffix in ("", "-wal", "-shm"):
        candidate = settings.db_path.with_name(settings.db_path.name + suffix)
        if candidate.exists():
            assert sentinel.encode() not in candidate.read_bytes()
