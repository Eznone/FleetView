"""Owning the terminal taps for a fleet, with tmux as the registry.

**tmux is the registry.** FleetView persists no mapping from agent to session.
It asks tmux, because tmux already knows: §4 chose it precisely so sessions
outlive the daemon, and libtmux sets ``FLEETVIEW_AGENT_ID`` in the session
environment at spawn, where ``show-environment`` can read it back. So
:meth:`TerminalSupervisor.reconcile` *discovers* the fleet rather than
remembering it — no state file, no registry table, and no class of stale rows
pointing at sessions that ended while the daemon was down.

``POST /v1/terminals`` exists only as the fast path, so the CLI does not wait a
poll interval after spawning. Everything it does, reconcile would do anyway.

**What this does not do.** It does not spawn, does not enforce the concurrency
cap, and emits no ``agent.lifecycle.*``. Spawning still lives in the CLI. The
real agent registry is Phase 3's; this is the smallest thing that lets the
daemon own a FIFO.

**Daemon restart, with agents live.** Our fds die with the process, tmux's
``cat`` gets EPIPE on its next write and exits, and tmux closes the pipe. On
startup reconcile finds the session with ``pane_pipe == 0`` and re-attaches.
Bytes produced in that window are lost — inherent, and stated here so it is
known rather than discovered.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import libtmux

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.schema.events import EventType, FleetViewEvent
from fleetview.spawn.tmux import (
    TerminalTapError,
    attach_pipe_pane,
    detach_pipe_pane,
    find_agent_sessions,
)
from fleetview.store import EventStore, TerminalChunkStore
from fleetview.terminal.fifo import FifoReader
from fleetview.terminal.paths import fifo_path
from fleetview.terminal.writer import LogWriter, output_topic

log = logging.getLogger(__name__)


@dataclass
class TerminalHandle:
    agent_id: str
    tmux_session: str
    fifo: Any
    reader: FifoReader
    writer: LogWriter
    attached_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TerminalSupervisor:
    """Attaches, reconciles and tears down one tap per live agent."""

    def __init__(
        self,
        settings: Settings,
        *,
        chunks: TerminalChunkStore,
        events: EventStore,
        bus: EventBus,
        run_id: str,
        server: libtmux.Server | None = None,
    ) -> None:
        self._settings = settings
        self._chunks = chunks
        self._events = events
        self._bus = bus
        self._run_id = run_id
        self._server = server
        self._handles: dict[str, TerminalHandle] = {}
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._settings.ensure_directories()
        await self.reconcile()
        self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for agent_id in list(self._handles):
            await self._teardown(agent_id)

    async def _poll(self) -> None:
        interval = self._settings.terminal_reconcile_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reconcile()
            except Exception:  # pragma: no cover - defensive
                log.exception("terminal reconcile failed")

    # --- attaching ---------------------------------------------------------

    async def attach(self, agent_id: str, tmux_session: str) -> TerminalHandle:
        """Tap an agent's pane. Idempotent — a second call is a no-op.

        Idempotent on purpose: re-attaching must never be the thing that turns
        capture off. That is the ``-o`` trap one level up (see
        :func:`fleetview.spawn.tmux.attach_pipe_pane`).
        """
        async with self._lock:
            existing = self._handles.get(agent_id)
            if existing is not None:
                return existing

            session = await asyncio.to_thread(self._find_session, tmux_session)
            if session is None:
                raise TerminalTapError(f"no tmux session named {tmux_session!r}")

            path = fifo_path(self._settings, agent_id)
            writer = LogWriter(
                agent_id,
                settings=self._settings,
                chunks=self._chunks,
                bus=self._bus,
                emit=lambda t, p, _a=agent_id: self._emit(_a, t, p),
            )
            await writer.start()

            topic = output_topic(agent_id)
            reader = FifoReader(
                path,
                on_bytes=lambda data, _t=topic: self._bus.publish(_t, data),
                read_size=self._settings.terminal_read_buffer_bytes,
            )
            reader.start()

            try:
                # Every libtmux call shells out synchronously. A 30 ms block on
                # the loop would show up as a latency spike on the ingest path.
                await asyncio.to_thread(attach_pipe_pane, session, path)
            except Exception as exc:
                reader.stop()
                await writer.stop()
                self._emit(agent_id, EventType.TERMINAL_TAP_FAILED,
                           {"session": tmux_session, "reason": str(exc)})
                raise

            handle = TerminalHandle(agent_id, tmux_session, path, reader, writer)
            self._handles[agent_id] = handle
            log.info("terminal tap attached: %s -> %s", agent_id, tmux_session)
            return handle

    async def detach(self, agent_id: str) -> bool:
        async with self._lock:
            if agent_id not in self._handles:
                return False
            await self._teardown(agent_id)
            return True

    async def _teardown(self, agent_id: str) -> None:
        """Shut one tap down in the order that does not lose the tail.

        Detach the pipe first so the drain is not racing a live writer; then
        the reader (which drains and unlinks the FIFO); then the writer, which
        indexes the final byte range. Any other order loses bytes, the index
        for them, or both.
        """
        handle = self._handles.pop(agent_id, None)
        if handle is None:
            return

        session = await asyncio.to_thread(self._find_session, handle.tmux_session)
        if session is not None:
            await asyncio.to_thread(detach_pipe_pane, session)

        with contextlib.suppress(Exception):
            handle.reader.stop()
        await handle.writer.stop()
        log.info("terminal tap detached: %s", agent_id)

    # --- discovery ---------------------------------------------------------

    async def reconcile(self) -> dict[str, str]:
        """Make our taps match the live tmux sessions."""
        live = await asyncio.to_thread(self._find_agents)

        for agent_id in list(self._handles):
            if agent_id not in live:
                async with self._lock:
                    await self._teardown(agent_id)

        for agent_id, session_name in live.items():
            if agent_id in self._handles:
                continue
            try:
                await self.attach(agent_id, session_name)
            except Exception as exc:
                log.warning("could not tap %s (%s): %s", agent_id, session_name, exc)
        return live

    def _server_or_default(self) -> libtmux.Server:
        return self._server if self._server is not None else libtmux.Server()

    def _find_agents(self) -> dict[str, str]:
        return find_agent_sessions(self._server_or_default())

    def _find_session(self, name: str):
        try:
            for session in self._server_or_default().sessions:
                if session.name == name:
                    return session
        except Exception:
            return None
        return None

    # --- reporting ---------------------------------------------------------

    def _emit(self, agent_id: str, event_type: EventType, payload: dict[str, Any]) -> None:
        self._events.append(
            FleetViewEvent(
                run_id=self._run_id,
                agent_id=agent_id,
                channel="terminal",
                event_type=event_type,
                payload=payload,
            )
        )

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "agentId": h.agent_id,
                "tmuxSession": h.tmux_session,
                "fifo": str(h.fifo),
                "attachedAt": h.attached_at.isoformat(),
                "bytesRead": h.reader.bytes_read,
                **h.writer.stats,
            }
            for h in self._handles.values()
        ]

    @property
    def count(self) -> int:
        return len(self._handles)


class NullTerminalSupervisor:
    """Stands in when capture is disabled, so nothing upstream needs an `if`."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def attach(self, agent_id: str, tmux_session: str):
        raise TerminalTapError("terminal capture is disabled (FLEETVIEW_TERMINAL_CAPTURE=0)")

    async def detach(self, agent_id: str) -> bool:
        return False

    async def reconcile(self) -> dict[str, str]:
        return {}

    def status(self) -> list[dict[str, Any]]:
        return []

    @property
    def count(self) -> int:
        return 0
