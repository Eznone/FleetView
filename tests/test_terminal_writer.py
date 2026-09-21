"""The LogWriter: authenticity, coalescing, and §9 risk 8's degradation.

Channel B is the only component in FleetView that can fill a disk, so the cap
is enforced by the writer rather than a sweeper. The tests that matter most
here are the ones where something goes *wrong* — a full disk, an unlinkable
segment, a bus that dropped buffers — because the design claim is about how it
fails, not how it succeeds.
"""

from __future__ import annotations

import asyncio
import os
import random
import stat

import pytest

from fleetview.bus import EventBus
from fleetview.config import DIR_MODE, FILE_MODE, Settings
from fleetview.schema.events import EventType
from fleetview.store import TerminalChunkStore, apply_schema, connect
from fleetview.terminal.paths import agent_log_dir, list_segments
from fleetview.terminal.writer import LogWriter, output_topic

AGENT = "agent-1"


class Harness:
    def __init__(self, conn, chunks, writer, bus, events, settings):
        self.conn, self.chunks, self.writer = conn, chunks, writer
        self.bus, self.events, self.settings = bus, events, settings

    async def feed(self, *buffers: bytes) -> None:
        """Publish through the real bus, then let the consumer drain it."""
        for buf in buffers:
            self.bus.publish(output_topic(AGENT), buf)
        for _ in range(200):
            if self.writer._sub.queue.empty():
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    def types(self):
        return [t for t, _ in self.events]

    def payloads(self, event_type):
        return [p for t, p in self.events if t == event_type]

    @property
    def log_dir(self):
        return agent_log_dir(self.settings, AGENT)

    def on_disk(self) -> bytes:
        return b"".join(p.read_bytes() for p in list_segments(self.log_dir))

    async def close(self):
        await self.writer.stop()
        await self.chunks.stop()
        await self.conn.close()


async def harness(tmp_path, **overrides) -> Harness:
    settings = Settings(home=tmp_path, **overrides)
    settings.ensure_directories()
    conn = await connect(settings.db_path)
    await apply_schema(conn)
    chunks = TerminalChunkStore(conn, settings=settings)
    await chunks.start()
    bus = EventBus()
    events: list = []
    writer = LogWriter(
        AGENT, settings=settings, chunks=chunks, bus=bus,
        emit=lambda t, p: events.append((t, p)),
    )
    await writer.start()
    return Harness(conn, chunks, writer, bus, events, settings)


# --- authenticity ------------------------------------------------------------

async def test_bytes_on_disk_are_identical_to_what_was_published(tmp_path):
    """§2.5's whole claim. Irregular buffer sizes, because a real pane emits
    whatever the TUI happened to paint."""
    h = await harness(tmp_path, terminal_segment_max_bytes=256 * 1024)
    rng = random.Random(1234)
    source = bytes(rng.getrandbits(8) for _ in range(512 * 1024))

    pos = 0
    while pos < len(source):
        size = rng.choice([1, 17, 512, 4096, 65536])
        await h.feed(source[pos:pos + size])
        pos += size

    await h.writer.stop()
    assert h.on_disk() == source
    await h.chunks.stop()
    await h.conn.close()


async def test_rows_tile_each_file_and_never_span_two(tmp_path):
    h = await harness(tmp_path, terminal_segment_max_bytes=64 * 1024,
                      terminal_chunk_bytes=8 * 1024)
    for _ in range(64):
        await h.feed(b"x" * 4096)
    await h.writer.stop()
    await h.chunks.flush()

    rows = await h.chunks.fetch(AGENT, limit=10_000)
    assert rows, "nothing was indexed"

    by_path: dict[str, list] = {}
    for row in rows:
        by_path.setdefault(row.path, []).append(row)

    for path, group in by_path.items():
        assert group[0].byte_offset == 0, f"{path} is not indexed from 0"
        for prev, nxt in zip(group, group[1:]):
            assert prev.end == nxt.byte_offset, f"{path} has a gap or overlap"
        # And the index agrees with the file it names.
        assert group[-1].end <= os.path.getsize(path)

    await h.chunks.stop()
    await h.conn.close()


async def test_a_chunk_row_covers_only_bytes_that_reached_disk(tmp_path):
    h = await harness(tmp_path)
    await h.feed(b"hello world")
    h.writer._flush_chunk()
    await h.chunks.flush()

    (row,) = await h.chunks.fetch(AGENT)
    with open(row.path, "rb") as fh:
        fh.seek(row.byte_offset)
        assert fh.read(row.length) == b"hello world"
    await h.close()


# --- coalescing --------------------------------------------------------------

async def test_the_index_does_not_grow_at_the_byte_rate(tmp_path):
    """Row-per-read would keep the bytes out of SQLite while letting the index
    grow at the byte rate — §4.1's scaling mistake one level up."""
    h = await harness(tmp_path, terminal_segment_max_bytes=32 * 1024 * 1024)
    for _ in range(1280):               # 1280 x 4 KiB = 5 MiB, 1280 reads
        await h.feed(b"y" * 4096)
    await h.writer.stop()
    await h.chunks.flush()

    rows = await h.chunks.count(AGENT)
    assert h.writer.bytes_written == 5 * 1024 * 1024
    assert rows < 100, f"{rows} rows for 5 MiB — the index is tracking reads"
    await h.chunks.stop()
    await h.conn.close()


async def test_a_quiet_pane_closes_its_open_run(tmp_path):
    """Otherwise a seek-by-time waits for the next 256 KiB, which may never come."""
    h = await harness(tmp_path, terminal_chunk_idle_ms=0)
    await h.feed(b"a short burst")
    assert await h.chunks.count(AGENT) == 0    # below the byte threshold

    h.writer.tick()
    await h.chunks.flush()
    assert await h.chunks.count(AGENT) == 1
    await h.close()


# --- rotation ----------------------------------------------------------------

async def test_rotation_produces_multiple_segments(tmp_path):
    h = await harness(tmp_path, terminal_segment_max_bytes=16 * 1024)
    for _ in range(20):
        await h.feed(b"z" * 4096)
    await h.writer.stop()

    segments = list_segments(h.log_dir)
    assert len(segments) >= 4
    assert EventType.TERMINAL_SEGMENT_ROTATED in h.types()
    # Sequence-first naming means write order is sort order.
    assert [p.name for p in segments] == sorted(p.name for p in segments)
    await h.chunks.stop()
    await h.conn.close()


async def test_a_restart_does_not_append_two_runs_into_one_file(tmp_path):
    h = await harness(tmp_path)
    await h.feed(b"first run")
    await h.writer.stop()
    first = list_segments(h.log_dir)[-1]

    second_writer = LogWriter(AGENT, settings=h.settings, chunks=h.chunks,
                              bus=h.bus, emit=lambda t, p: None)
    await second_writer.start()
    await second_writer.stop()

    segments = list_segments(h.log_dir)
    assert len(segments) == 2
    assert first.read_bytes() == b"first run"
    await h.chunks.stop()
    await h.conn.close()


# --- the cap (risk 8) --------------------------------------------------------

async def test_the_cap_is_enforced_and_the_newest_bytes_survive(tmp_path):
    h = await harness(tmp_path, terminal_segment_max_bytes=16 * 1024,
                      terminal_max_bytes_per_agent=64 * 1024)
    for i in range(60):                       # ~240 KiB, ~4x the cap
        await h.feed(bytes([i % 256]) * 4096)
    await h.writer.stop()
    await h.chunks.flush()

    on_disk = sum(p.stat().st_size for p in list_segments(h.log_dir))
    assert on_disk <= 64 * 1024 + 16 * 1024, f"{on_disk} bytes survived a 64 KiB cap"

    # The survivors are the newest: the last buffer written is still readable.
    assert h.on_disk().endswith(bytes([59 % 256]) * 4096)

    # And the index does not outlive the bytes.
    for row in await h.chunks.fetch(AGENT, limit=10_000):
        assert os.path.exists(row.path), f"row points at pruned {row.path}"
    await h.chunks.stop()
    await h.conn.close()


async def test_an_unprunable_segment_truncates_rather_than_filling_the_disk(tmp_path):
    """The risk-8 test. A rotation failure must degrade to truncation."""
    h = await harness(tmp_path, terminal_segment_max_bytes=8 * 1024,
                      terminal_max_bytes_per_agent=16 * 1024)

    original = os.unlink

    def refuse(path, *a, **kw):
        raise OSError(13, "Permission denied")

    for _ in range(4):
        await h.feed(b"q" * 4096)          # build up past the cap first

    import pathlib
    monkey = pathlib.Path.unlink
    pathlib.Path.unlink = lambda self, **kw: refuse(self)
    try:
        for _ in range(40):
            await h.feed(b"w" * 4096)
        size_after_failure = sum(p.stat().st_size for p in list_segments(h.log_dir))
        for _ in range(40):
            await h.feed(b"w" * 4096)
        size_later = sum(p.stat().st_size for p in list_segments(h.log_dir))
    finally:
        pathlib.Path.unlink = monkey

    assert h.writer._truncating is True
    assert EventType.TERMINAL_TRUNCATED in h.types()
    assert h.types().count(EventType.TERMINAL_TRUNCATED) == 1, "said more than once"
    assert h.writer.bytes_dropped > 0
    assert size_later == size_after_failure, "disk kept growing while truncating"
    await h.chunks.stop()
    await h.conn.close()


async def test_a_full_disk_does_not_raise_into_the_reader(tmp_path):
    """ENOSPC on write. The agent must keep running; only telemetry is lost."""
    h = await harness(tmp_path)

    class FullDisk:
        def write(self, data):
            raise OSError(28, "No space left on device")
        def flush(self):
            pass
        def close(self):
            pass

    h.writer._file = FullDisk()
    await h.feed(b"this cannot land")      # must not raise

    assert h.writer._truncating is True
    assert EventType.TERMINAL_TRUNCATED in h.types()
    assert h.writer.bytes_dropped == len(b"this cannot land")
    h.writer._file = None
    await h.chunks.stop()
    await h.conn.close()


# --- the gap rule ------------------------------------------------------------

async def test_a_bus_drop_becomes_a_recorded_gap(tmp_path):
    """§2.2's topology is kept, so drops are possible. They must not be silent:
    an undetectable hole is F2's shape exactly."""
    h = await harness(tmp_path, terminal_bus_queue_size=4)

    # Overflow the subscription without letting the consumer run.
    for i in range(40):
        h.bus.publish(output_topic(AGENT), b"buffer-%02d" % i)
    assert h.writer._sub.dropped > 0

    while not h.writer._sub.queue.empty():
        _t, data = h.writer._sub.queue.get_nowait()
        await h.writer._handle(data)

    assert h.writer.gaps == 1
    (payload,) = h.payloads(EventType.TERMINAL_GAP)
    assert payload["dropped_buffers"] == h.writer._sub.dropped
    await h.close()


async def test_no_chunk_row_spans_a_gap(tmp_path):
    """Each row's created_at must stay honest about the bytes it covers."""
    h = await harness(tmp_path, terminal_bus_queue_size=4,
                      terminal_chunk_bytes=10 * 1024 * 1024)
    await h.feed(b"before the gap")

    for i in range(40):
        h.bus.publish(output_topic(AGENT), b"x" * 16)
    while not h.writer._sub.queue.empty():
        _t, data = h.writer._sub.queue.get_nowait()
        await h.writer._handle(data)

    await h.writer.stop()
    await h.chunks.flush()

    rows = await h.chunks.fetch(AGENT, limit=100)
    assert len(rows) >= 2, "the gap did not close the open run"
    assert rows[0].length == len(b"before the gap")
    await h.chunks.stop()
    await h.conn.close()


# --- permissions -------------------------------------------------------------

async def test_segments_and_their_directory_stay_private(tmp_path):
    h = await harness(tmp_path)
    await h.feed(b"secret-looking output")
    await h.writer.stop()

    assert stat.S_IMODE(h.log_dir.stat().st_mode) == DIR_MODE
    for segment in list_segments(h.log_dir):
        assert stat.S_IMODE(segment.stat().st_mode) == FILE_MODE
    await h.chunks.stop()
    await h.conn.close()


# --- lifecycle events --------------------------------------------------------

async def test_tap_opened_and_closed_bracket_the_writers_life(tmp_path):
    h = await harness(tmp_path)
    assert h.types()[0] == EventType.TERMINAL_TAP_OPENED
    await h.feed(b"work")
    await h.writer.stop()

    assert h.types()[-1] == EventType.TERMINAL_TAP_CLOSED
    closed = h.payloads(EventType.TERMINAL_TAP_CLOSED)[0]
    assert closed["bytes_written"] == 4
    await h.chunks.stop()
    await h.conn.close()


async def test_stop_indexes_the_final_run(tmp_path):
    """A lost final row is a byte range nothing can find again."""
    h = await harness(tmp_path)
    await h.feed(b"the last thing it said")
    await h.writer.stop()
    await h.chunks.flush()

    rows = await h.chunks.fetch(AGENT)
    assert rows and rows[-1].length == len(b"the last thing it said")
    await h.chunks.stop()
    await h.conn.close()
