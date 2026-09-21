"""The LogWriter — flat files, writer-enforced rotation, and the byte index.

This is §9 risk 8's module. Channel B is the only component in FleetView that
can fill a disk: 50-500 KB/min per agent, a 15-agent day in the gigabytes. So
the size cap is enforced **here, on the write path**, not by a sweeper. The
difference is the failure mode. A sweeper that dies leaves the disk filling
silently; a writer that cannot prune stops writing and says so with a
``terminal.truncated`` event. Degrading to truncation is the designed
behaviour, not an accident.

**Why a bus subscriber.** §2.2 reproduces CAO's topology -- ``FifoReader``
publishes, ``LogWriter`` subscribes -- and FleetView keeps it. The cost is that
:class:`~fleetview.bus.Subscription` drops the *oldest* item when its queue
fills, so bytes can be lost between the pane and the file.

That cost is paid with eyes open, and paid *visibly*: the bus counts every drop,
this writer reads that counter before every write, and a move emits
``terminal.gap``. An undetected hole would be the worst possible bug in a file
whose whole selling point (§2.5) is being byte-for-byte authentic -- and it is
exactly Phase 0 finding F2's shape, success reported and work not done. The
queue is also sized from the real byte rate rather than the bus default; see
``Settings.terminal_bus_queue_size``.

**What a gap does and does not mean.** The *file* stays contiguous -- byte N is
still followed by byte N+1, and a reader can still seek by offset. What is lost
is fidelity to the pane: some of what the agent printed never reached disk. So a
gap closes the open chunk row rather than corrupting anything, which keeps each
row's ``created_at`` honest about the bytes it actually covers and records where
the loss happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fleetview.bus import EventBus, Subscription
from fleetview.config import FILE_MODE, Settings
from fleetview.schema.events import EventType
from fleetview.store.terminal import TerminalChunkStore
from fleetview.terminal.paths import (
    agent_log_dir,
    list_segments,
    next_sequence,
    open_segment,
    total_bytes,
)

log = logging.getLogger(__name__)

#: Bus topic carrying one agent's raw pane bytes. §2.2's `terminal.{id}.output`.
def output_topic(agent_id: str) -> str:
    return f"terminal.{agent_id}.output"


class LogWriter:
    """Appends one agent's pane bytes to rotating flat files, and indexes them."""

    def __init__(
        self,
        agent_id: str,
        *,
        settings: Settings,
        chunks: TerminalChunkStore,
        bus: EventBus,
        emit: Callable[[EventType, dict[str, Any]], None] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._settings = settings
        self._chunks = chunks
        self._bus = bus
        self._emit_event = emit or (lambda _t, _p: None)

        self._dir: Path = agent_log_dir(settings, agent_id)
        self._sub: Subscription | None = None
        self._task: asyncio.Task[None] | None = None

        self._file = None
        self._segment_path: Path | None = None
        self._segment_seq = 0
        self._segment_bytes = 0
        self._total_bytes = 0

        # The open, not-yet-indexed byte run.
        self._chunk_path: Path | None = None
        self._chunk_offset = 0
        self._chunk_length = 0
        self._chunk_started_at: datetime | None = None
        self._last_byte_at = 0.0

        self._last_dropped = 0
        self._truncating = False

        self.bytes_written = 0
        self.bytes_dropped = 0
        self.gaps = 0
        self.rotations = 0
        self.chunks_indexed = 0

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._total_bytes = total_bytes(self._dir)
        self._segment_seq = next_sequence(self._dir)
        self._open_segment()

        self._sub = self._bus.subscribe(
            output_topic(self._agent_id),
            maxsize=self._settings.terminal_bus_queue_size,
        )
        self._task = asyncio.create_task(self._consume())
        self._emit_event(
            EventType.TERMINAL_TAP_OPENED,
            {"path": str(self._segment_path), "segment": self._segment_seq},
        )

    async def stop(self) -> None:
        """Drain what the bus still holds, index the final run, close.

        Draining before closing matters: the last chunk row is the only record
        of where the final bytes are, and the file carries no index of its own.
        """
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if self._sub is not None:
            while not self._sub.queue.empty():
                _topic, data = self._sub.queue.get_nowait()
                await self._handle(data)
            self._bus.unsubscribe(self._sub)
            self._sub = None

        self._flush_chunk()
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.flush()
                self._file.close()
            self._file = None

        self._emit_event(
            EventType.TERMINAL_TAP_CLOSED,
            {"path": str(self._segment_path), **self.stats},
        )

    async def _consume(self) -> None:
        assert self._sub is not None
        tick = self._settings.terminal_tick_ms / 1000
        while True:
            try:
                _topic, data = await asyncio.wait_for(self._sub.queue.get(), timeout=tick)
            except (asyncio.TimeoutError, TimeoutError):
                # Quiet pane: close the open run so a seek-by-time lands on it
                # rather than waiting for the next 256 KiB that may never come.
                self.tick()
                continue
            try:
                await self._handle(data)
            except Exception:  # pragma: no cover - defensive
                # Never let one bad buffer kill the writer and silently stop
                # capture while the agent keeps running.
                log.exception("terminal writer failed on a buffer (%s)", self._agent_id)

    # --- the write path ----------------------------------------------------

    async def _handle(self, data: bytes) -> None:
        if not data:
            return

        self._check_gap()

        if self._truncating:
            await self._prune()
            if self._truncating:
                self.bytes_dropped += len(data)
                return

        if self._segment_bytes >= self._settings.terminal_segment_max_bytes:
            await self._rotate()

        if self._file is None:
            self.bytes_dropped += len(data)
            return

        try:
            self._file.write(data)
            self._file.flush()
        except OSError as exc:
            # ENOSPC and friends. Stop writing, say so, keep the agent running.
            self._enter_truncating(f"write failed: {exc}")
            self.bytes_dropped += len(data)
            return

        if self._chunk_started_at is None:
            self._begin_chunk()
        self._chunk_length += len(data)
        self._segment_bytes += len(data)
        self._total_bytes += len(data)
        self.bytes_written += len(data)
        self._last_byte_at = time.monotonic()

        if self._chunk_length >= self._settings.terminal_chunk_bytes:
            self._flush_chunk()

    def _check_gap(self) -> None:
        """Turn the bus's silent overflow into a recorded fact."""
        if self._sub is None or self._sub.dropped <= self._last_dropped:
            return
        lost = self._sub.dropped - self._last_dropped
        self._last_dropped = self._sub.dropped
        self.gaps += 1
        # Close the open run first: the bytes after the gap are not continuous
        # with it *in time*, and a row whose created_at spans the loss would
        # make seek-by-time point at the wrong place.
        self._flush_chunk()
        self._emit_event(
            EventType.TERMINAL_GAP,
            {
                "path": str(self._segment_path),
                "byte_offset": self._segment_bytes,
                "dropped_buffers": lost,
            },
        )
        log.warning(
            "terminal gap for %s: %d buffer(s) dropped by the bus", self._agent_id, lost
        )

    def tick(self, now: float | None = None) -> None:
        """Index an open run that has gone quiet. Safe to call at any time."""
        if self._chunk_length <= 0:
            return
        now = time.monotonic() if now is None else now
        if (now - self._last_byte_at) * 1000 >= self._settings.terminal_chunk_idle_ms:
            self._flush_chunk()

    # --- segments ----------------------------------------------------------

    def _open_segment(self) -> None:
        self._segment_path = open_segment(self._dir, self._segment_seq, mode=FILE_MODE)
        self._segment_bytes = self._segment_path.stat().st_size
        self._file = open(self._segment_path, "ab")

    async def _rotate(self) -> None:
        # A row names exactly one file, so the open run is indexed against the
        # segment it actually lives in before anything else happens.
        self._flush_chunk()
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.flush()
                self._file.close()
            self._file = None

        previous = self._segment_path
        self._segment_seq += 1
        self._open_segment()
        self.rotations += 1
        self._emit_event(
            EventType.TERMINAL_SEGMENT_ROTATED,
            {
                "previous": str(previous),
                "path": str(self._segment_path),
                "segment": self._segment_seq,
            },
        )
        await self._prune()

    async def _prune(self) -> None:
        """Enforce 48 h / 200 MB per agent, oldest first, never the current
        segment.

        This is the cap's only enforcement point. If it cannot do its job the
        writer enters truncating mode rather than letting the directory grow.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._settings.terminal_retention_hours
        )
        removed: list[str] = []

        for segment in list_segments(self._dir):
            if segment == self._segment_path:
                break  # never the file we are writing
            try:
                stat = segment.stat()
            except OSError:
                continue
            too_big = self._total_bytes > self._settings.terminal_max_bytes_per_agent
            too_old = datetime.fromtimestamp(stat.st_mtime, timezone.utc) < cutoff
            if not (too_big or too_old):
                break
            try:
                segment.unlink()
            except OSError as exc:
                self._enter_truncating(f"could not prune {segment.name}: {exc}")
                break
            self._total_bytes -= stat.st_size
            removed.append(str(segment))

        if removed:
            # The index must not outlive the bytes: a row pointing at a deleted
            # file is a broken pointer, and the pinned column set gives us no
            # tombstone flag to mark one with.
            #
            # Scheduled rather than awaited. The forget goes into the chunk
            # store's own queue *behind* the inserts that named these segments,
            # so ordering is the queue's job and this path stays free of awaits.
            # An earlier version awaited a flush here instead and deadlocked the
            # writer mid-prune: the segment vanished, its rows did not, and
            # capture stopped with nothing logged.
            self._chunks.schedule_forget(removed)

        if (
            self._truncating
            and self._total_bytes <= self._settings.terminal_max_bytes_per_agent
            and self._file is not None
        ):
            self._truncating = False
            self._emit_event(
                EventType.TERMINAL_RESUMED,
                {"path": str(self._segment_path), "bytes_dropped": self.bytes_dropped},
            )

    def _enter_truncating(self, reason: str) -> None:
        """Say it once, loudly. Risk 8's degradation must never be silent."""
        if self._truncating:
            return
        self._truncating = True
        log.error("terminal capture truncating for %s: %s", self._agent_id, reason)
        self._emit_event(
            EventType.TERMINAL_TRUNCATED,
            {
                "path": str(self._segment_path),
                "reason": reason,
                "bytes_on_disk": self._total_bytes,
                "cap": self._settings.terminal_max_bytes_per_agent,
            },
        )

    # --- the chunk index ---------------------------------------------------

    def _begin_chunk(self) -> None:
        self._chunk_path = self._segment_path
        self._chunk_offset = self._segment_bytes
        self._chunk_length = 0
        # First byte's time, not the flush time: this is what seek-by-time reads.
        self._chunk_started_at = datetime.now(timezone.utc)

    def _flush_chunk(self) -> None:
        if self._chunk_length > 0 and self._chunk_path is not None:
            self._chunks.append(
                agent_id=self._agent_id,
                path=str(self._chunk_path),
                byte_offset=self._chunk_offset,
                length=self._chunk_length,
                created_at=self._chunk_started_at or datetime.now(timezone.utc),
            )
            self.chunks_indexed += 1
        self._chunk_path = None
        self._chunk_length = 0
        self._chunk_started_at = None

    # --- introspection -----------------------------------------------------

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "bytes_written": self.bytes_written,
            "bytes_dropped": self.bytes_dropped,
            "gaps": self.gaps,
            "rotations": self.rotations,
            "chunks": self.chunks_indexed,
            "bytes_on_disk": self._total_bytes,
            "truncating": self._truncating,
            "segment": self._segment_seq,
        }
