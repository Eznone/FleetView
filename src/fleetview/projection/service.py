"""Keeping the projection current, off the ingest path.

§4.1 tier 3 makes the in-memory projection authoritative for the UI, so
something has to feed it. That something is a **bus subscriber**, never the
ingest handler: :mod:`fleetview.store.events` already fans out after the
commit, and :mod:`fleetview.bus` is built so a slow subscriber falls behind and
recovers rather than applying backpressure all the way to a hook shim that is
holding a worker's tool call open.

The service also owns the one transition no event announces. §5.3's
`waiting_on_tool` is "`PreToolUse` with no `PostToolUse` past threshold" -- a
property of elapsed time -- so a periodic task looks at the clock. It is
deliberately a *separate* task from the fold, for the same reason §4.1 keeps
pruning off the ingest path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.projection.fleet import FleetProjection
from fleetview.schema.events import FleetViewEvent
from fleetview.store import EventStore

log = logging.getLogger(__name__)

#: Bus topic carrying "the canvas moved". Distinct from `events.*` so a UI
#: client can take state changes without also taking the raw feed -- and, more
#: to the point, without a prefix broad enough to pull in `terminal.*`, whose
#: items are 64 KiB byte buffers.
PROJECTION_TOPIC = "projection.agents"


class ProjectionService:
    """Owns a :class:`FleetProjection` and the two tasks that keep it live."""

    def __init__(
        self,
        *,
        settings: Settings,
        bus: EventBus,
        store: EventStore | None = None,
        run_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._store = store
        self.fleet = FleetProjection(settings=settings, run_id=run_id)
        self._sub = None
        self._tasks: list[asyncio.Task] = []
        self._dirty = False

    async def start(self) -> None:
        # Subscribe *before* replaying. The other order loses every event that
        # commits while the replay query is in flight, and loses it silently --
        # the canvas would simply be missing an agent until its next event.
        # Re-applying an event we also replayed is harmless, because the fold
        # is idempotent for state and `event_count` is not rendered.
        self._sub = self._bus.subscribe(
            "events", maxsize=self._settings.projection_bus_queue_size
        )

        if self._store is not None:
            replayed = await self._store.fetch(
                limit=self._settings.projection_replay_limit, newest=True
            )
            self.fleet.rebuild(replayed)
            log.info("projection seeded from %d events", len(replayed))

        self._tasks = [
            asyncio.create_task(self._consume(), name="projection-consume"),
            asyncio.create_task(self._tick(), name="projection-tick"),
            asyncio.create_task(self._broadcast(), name="projection-broadcast"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        if self._dirty:
            self._dirty = False
            self._announce()
        if self._sub is not None:
            self._bus.unsubscribe(self._sub)
            self._sub = None

    async def _consume(self) -> None:
        assert self._sub is not None
        while True:
            _topic, event = await self._sub.queue.get()
            if not isinstance(event, FleetViewEvent):  # pragma: no cover - defensive
                continue
            try:
                if self.fleet.apply(event):
                    # Mark, do not publish. Building a snapshot here costs the
                    # §7 load gate ~9% of its throughput; `_broadcast` does it
                    # at most once per `projection_broadcast_ms` instead.
                    self._dirty = True
            except Exception:  # pragma: no cover - the canvas is not worth a daemon
                log.exception("projection failed to apply %s", event.event_type)

    async def _tick(self) -> None:
        interval = self._settings.projection_tick_ms / 1000.0
        while True:
            await asyncio.sleep(interval)
            try:
                if self.fleet.tick():
                    self._dirty = True
            except Exception:  # pragma: no cover
                log.exception("projection tick failed")

    async def _broadcast(self) -> None:
        """Publish a coalesced snapshot, at most once per interval.

        The fold runs per event and is cheap; *serialising* the fleet is not,
        so the two are deliberately decoupled. Under a burst the operator sees
        the latest state rather than every intermediate one, which is what they
        would want even if it were free.
        """
        interval = self._settings.projection_broadcast_ms / 1000.0
        while True:
            await asyncio.sleep(interval)
            if not self._dirty:
                continue
            self._dirty = False
            try:
                self._announce()
            except Exception:  # pragma: no cover
                log.exception("projection broadcast failed")

    def _announce(self) -> None:
        """Publish the whole fleet, not a per-agent delta.

        A delta would be smaller, but it would also need its own ordering
        rules, its own edge-change vocabulary and a client able to merge both.
        §8.5 caps the fleet at 3 agents, so a snapshot is a handful of small
        objects -- and a client that simply replaces its state cannot drift out
        of sync with the daemon, which a merge can.
        """
        self._bus.publish(PROJECTION_TOPIC, self.fleet.snapshot())
