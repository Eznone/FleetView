"""The tier-2 index — where an agent's terminal bytes are, never what they are.

§4.1 names row-per-chunk terminal bytes as *the* scaling mistake this schema
invites. The bytes go to flat append-only files; this table stores
``{path, byte_offset, length}`` and nothing else, and
``tests/test_store.py`` pins both halves of that.

**What a row means.** A chunk row is a *time index into a contiguous file*, not
a unit of data. The bytes are already recoverable with ``cat seg-*.log``; the
one question a flat file cannot answer is "where in these 40 MB was 14:03?".
So rows are coalesced — one per 256 KiB, or per second of quiet, or per
segment, whichever comes first. A row per FIFO read would keep the bytes out of
the database while letting the *index* grow at the byte rate, which is the same
mistake one level up.

``byte_offset`` is relative to its own segment file, so ``{path, byte_offset,
length}`` is self-contained and a file's rows tile it from 0 with no gaps. A
row therefore may never span two files, nor span a gap in the byte stream —
see :mod:`fleetview.terminal.writer`.

**Two writers, one connection.** This store and :class:`~fleetview.store.events.EventStore`
share the daemon's single :class:`aiosqlite.Connection`, so either one's
``commit()`` also commits the other's pending inserts. That is safe and worth
stating rather than leaving to be rediscovered: both are pure appends with no
read-modify-write, so there is no lost-update class, and aiosqlite serialises
every statement onto its own thread. At ~2 commits/min from this side the extra
transaction cost is nil.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import aiosqlite

from fleetview.config import Settings

log = logging.getLogger(__name__)

_INSERT = """
INSERT INTO terminal_chunks (agent_id, path, byte_offset, length, created_at)
VALUES (?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class TerminalChunk:
    """One indexed byte range within one segment file."""

    id: int
    agent_id: str
    path: str
    byte_offset: int
    length: int
    created_at: str

    @property
    def end(self) -> int:
        return self.byte_offset + self.length


def _row_to_chunk(row: aiosqlite.Row) -> TerminalChunk:
    return TerminalChunk(
        id=row["id"],
        agent_id=row["agent_id"],
        path=row["path"],
        byte_offset=row["byte_offset"],
        length=row["length"],
        created_at=row["created_at"],
    )


class TerminalChunkStore:
    """Batched writer and reader for ``terminal_chunks``.

    Deliberately not folded into :class:`EventStore`. That class assigns a
    per-run ``sequence`` inside its transaction, which is what makes event
    ordering authoritative; chunks have no run and no sequence, and widening
    its queue to a union type would blur the one module that is meant to stay
    narrow.
    """

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings or Settings()
        self._queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop())

    async def stop(self) -> None:
        """Drain what is queued, then stop.

        Never drops: a lost final row is a byte range that nothing can find
        again, because the file itself carries no index.
        """
        await self.flush()
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
            self._writer = None

    async def flush(self) -> None:
        await self._queue.join()

    # --- writing -----------------------------------------------------------

    def append(
        self,
        *,
        agent_id: str,
        path: str,
        byte_offset: int,
        length: int,
        created_at: datetime,
    ) -> None:
        """Enqueue an index row. Synchronous and non-blocking, like
        :meth:`EventStore.append` and for the same reason.

        ``created_at`` is the timestamp of the chunk's *first* byte, not of
        this call. Seeking by time is the row's whole purpose, and stamping it
        at flush would skew every seek by up to the coalescing window.
        """
        self._queue.put_nowait(
            ("insert", (agent_id, path, byte_offset, length, created_at.isoformat()))
        )

    async def _write_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            deadline = (
                asyncio.get_running_loop().time()
                + self._settings.flush_interval_ms / 1000
            )
            while len(batch) < self._settings.flush_max_events:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except (asyncio.TimeoutError, TimeoutError):
                    break
            try:
                await self._apply(batch)
                await self._conn.commit()
            except Exception:  # pragma: no cover - defensive
                # A failed write must not kill the writer and silently stop all
                # indexing while bytes keep landing on disk.
                log.exception("terminal chunk batch failed (%d ops)", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _apply(self, batch: list[tuple[str, object]]) -> None:
        """Apply a batch of ops **in order**, grouping consecutive inserts.

        Order is the whole point. A 'forget' for a pruned segment must land
        after the inserts that named it, or those rows resurrect the pointer it
        exists to remove — a row pointing at a file that is no longer there.
        Doing it through this queue rather than by awaiting a flush from the
        write path is what keeps the writer's hot path free of awaits.
        """
        pending: list[tuple] = []
        for kind, payload in batch:
            if kind == "insert":
                pending.append(payload)  # type: ignore[arg-type]
                continue
            if pending:
                await self._conn.executemany(_INSERT, pending)
                pending = []
            paths = list(payload)  # type: ignore[call-overload]
            if paths:
                placeholders = ",".join("?" * len(paths))
                await self._conn.execute(
                    f"DELETE FROM terminal_chunks WHERE path IN ({placeholders})", paths
                )
        if pending:
            await self._conn.executemany(_INSERT, pending)

    # --- reading -----------------------------------------------------------

    async def fetch(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        path: str | None = None,
        limit: int = 1000,
        newest: bool = False,
    ) -> list[TerminalChunk]:
        """Rows for one agent, oldest first — or the last ``limit`` when
        ``newest``, since a "tail" that returns the oldest rows of a long run
        is useless and quietly so."""
        clauses = ["agent_id = ?"]
        params: list = [agent_id]
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until.isoformat())
        if path is not None:
            clauses.append("path = ?")
            params.append(path)

        order = "id DESC" if newest else "id"
        sql = (
            f"SELECT * FROM terminal_chunks WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order} LIMIT ?"
        )
        params.append(limit)

        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        chunks = [_row_to_chunk(row) for row in rows]
        return list(reversed(chunks)) if newest else chunks

    async def segments(self, agent_id: str) -> list[str]:
        """Distinct segment paths for an agent, in write order."""
        async with self._conn.execute(
            "SELECT path, MIN(id) AS first_id FROM terminal_chunks "
            "WHERE agent_id = ? GROUP BY path ORDER BY first_id",
            (agent_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["path"] for row in rows]

    async def agents(self) -> list[str]:
        async with self._conn.execute(
            "SELECT DISTINCT agent_id FROM terminal_chunks ORDER BY agent_id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["agent_id"] for row in rows]

    def schedule_forget(self, paths: Iterable[str]) -> None:
        """Queue a forget behind whatever inserts are already pending.

        This is what the LogWriter calls after a prune. It is synchronous by
        design: awaiting anything from the write path is what deadlocked an
        earlier version of this code, and the queue already gives the ordering
        that await was reaching for.
        """
        paths = list(paths)
        if paths:
            self._queue.put_nowait(("forget", paths))

    async def forget_paths(self, paths: Iterable[str]) -> int:
        """Drop the rows for segments the writer has unlinked.

        A row pointing at a deleted file is a broken pointer, and the pinned
        column set gives us no tombstone flag to mark one with — so deleting
        the row is the honest option. Called by the writer immediately after a
        prune, in the same breath, so the index never outlives the bytes by
        more than a moment.
        """
        paths = list(paths)
        if not paths:
            return 0
        placeholders = ",".join("?" * len(paths))
        cursor = await self._conn.execute(
            f"DELETE FROM terminal_chunks WHERE path IN ({placeholders})", paths
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def count(self, agent_id: str | None = None) -> int:
        if agent_id is None:
            sql, params = "SELECT COUNT(*) AS n FROM terminal_chunks", ()
        else:
            sql, params = (
                "SELECT COUNT(*) AS n FROM terminal_chunks WHERE agent_id = ?",
                (agent_id,),
            )
        async with self._conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    @property
    def pending(self) -> int:
        """Rows enqueued but not yet committed. Exposed so tests and /v1/health
        do not have to reach into a private queue."""
        return self._queue.qsize()
