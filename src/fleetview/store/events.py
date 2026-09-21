"""The event repository — the only module in FleetView that writes SQL.

§4.1's escape hatch depends on this: because the store is event-sourced, moving
to Postgres later is "replay the log and rebuild projections" *provided* no
other layer has quietly grown its own SQL. That constraint costs nothing now
and is very expensive to reintroduce later, so it is worth being strict about.

**Group commit.** One writer task drains a queue and commits on a ~50 ms window
or a batch ceiling, whichever comes first. Producers never await a disk write:
:meth:`EventStore.append` is a synchronous enqueue. Measured headroom is ~3000x
the projected peak (§4.1), so this is not about throughput — it is about the
ingest path never blocking on fsync while a hook shim holds a worker's tool
call open.

**Sequence assignment happens here**, inside the transaction, because there is
exactly one writer. That is what makes ordering authoritative even though three
channels at three different latencies feed the same run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import aiosqlite

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.schema.events import FleetViewEvent

log = logging.getLogger(__name__)

_INSERT = """
INSERT INTO events (
    id, run_id, sequence, timestamp, trace_id, span_id, parent_span_id,
    agent_id, channel, event_type, payload, provider_metadata
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _row_to_event(row: aiosqlite.Row) -> FleetViewEvent:
    return FleetViewEvent(
        id=row["id"],
        run_id=row["run_id"],
        sequence=row["sequence"],
        timestamp=row["timestamp"],
        trace_id=row["trace_id"],
        span_id=row["span_id"],
        parent_span_id=row["parent_span_id"],
        agent_id=row["agent_id"],
        channel=row["channel"],
        event_type=row["event_type"],
        payload=json.loads(row["payload"]),
        provider_metadata=(
            json.loads(row["provider_metadata"]) if row["provider_metadata"] else None
        ),
    )


class EventStore:
    """Append-only event log with batched writes and bus fan-out."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        bus: EventBus | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._settings = settings or Settings()
        self._queue: asyncio.Queue[FleetViewEvent] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._sequences: dict[str, int] = {}

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())

    async def stop(self) -> None:
        """Drain what is queued, then stop. Never drops buffered events."""
        await self.flush()
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
            self._writer = None

    async def flush(self) -> None:
        """Block until everything queued has been committed."""
        await self._queue.join()

    # --- writing -----------------------------------------------------------

    def append(self, event: FleetViewEvent) -> None:
        """Enqueue an event. Synchronous and non-blocking, by design."""
        self._queue.put_nowait(event)

    async def _write_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            deadline = asyncio.get_running_loop().time() + self._settings.flush_interval_ms / 1000

            # Collect whatever else shows up inside the window.
            while len(batch) < self._settings.flush_max_events:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(
                        await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    break

            try:
                await self._commit(batch)
            except Exception:  # pragma: no cover - defensive
                # A failed write must not kill the writer task and silently
                # stop all ingest. Log loudly and keep serving.
                log.exception("event batch failed to commit (%d events)", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _commit(self, batch: list[FleetViewEvent]) -> None:
        rows = []
        for event in batch:
            event.sequence = await self._next_sequence(event.run_id)
            rows.append(
                (
                    event.id,
                    event.run_id,
                    event.sequence,
                    event.timestamp.isoformat(),
                    event.trace_id,
                    event.span_id,
                    event.parent_span_id,
                    event.agent_id,
                    str(event.channel),
                    str(event.event_type),
                    json.dumps(event.payload, default=str),
                    json.dumps(event.provider_metadata, default=str)
                    if event.provider_metadata is not None
                    else None,
                )
            )

        await self._conn.executemany(_INSERT, rows)
        await self._conn.commit()

        # Fan out only after the commit: a subscriber must never see an event
        # that is not yet durable, or a UI can show work the log will not have
        # after a crash.
        if self._bus is not None:
            for event in batch:
                topic = f"events.{event.agent_id}" if event.agent_id else "events"
                self._bus.publish(topic, event)

    async def _next_sequence(self, run_id: str) -> int:
        """Next sequence for a run, resumed from disk on first use.

        Reading MAX(sequence) once per run rather than per event is what keeps
        this off the hot path; single-writer is what makes the cached counter
        safe.
        """
        if run_id not in self._sequences:
            async with self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS s FROM events WHERE run_id = ?",
                (run_id,),
            ) as cursor:
                row = await cursor.fetchone()
            self._sequences[run_id] = int(row["s"]) if row else 0
        self._sequences[run_id] += 1
        return self._sequences[run_id]

    # --- reading -----------------------------------------------------------

    async def fetch(
        self,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        event_type: str | None = None,
        after_sequence: int | None = None,
        after_id: str | None = None,
        grep: str | None = None,
        limit: int = 1000,
        newest: bool = False,
    ) -> list[FleetViewEvent]:
        """Query the log. Backs ``fleetview events tail``.

        Ordering depends on the cursor. ``after_sequence`` orders by
        ``(run_id, sequence)`` — a run's own story, in its own order. Following
        the log live means ``after_id``, which orders by id instead: UUIDv7
        sorts by creation time, so that is a true chronological tail across
        several runs, where per-run sequences would interleave meaninglessly.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if after_sequence is not None:
            clauses.append("sequence > ?")
            params.append(after_sequence)
        if after_id is not None:
            clauses.append("id > ?")
            params.append(after_id)
        if grep is not None:
            # Matched against the JSON payload and the type, which is what
            # someone grepping a log actually means.
            clauses.append("(payload LIKE ? OR event_type LIKE ?)")
            params.extend([f"%{grep}%", f"%{grep}%"])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "id" if after_id is not None else "run_id, sequence"
        # `newest` takes the LAST n rather than the first: a command called
        # "tail" that hands back the oldest 100 events of a long run is
        # useless, and quietly so.
        sql = f"SELECT * FROM events {where} ORDER BY {order}{' DESC' if newest else ''} LIMIT ?"
        params.append(limit)

        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        events = [_row_to_event(row) for row in rows]
        return list(reversed(events)) if newest else events

    @property
    def pending(self) -> int:
        """Events enqueued but not yet committed.

        Exposed rather than left as a private queue because it is the ingest
        path's stall signal: throughput that looks fine while this grows
        monotonically means the daemon is buffering, not keeping up. The load
        gate asserts on it, and a test reaching into ``_queue`` would pin a
        private.
        """
        return self._queue.qsize()

    async def count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) AS n FROM events") as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0
