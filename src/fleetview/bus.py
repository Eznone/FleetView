"""The in-process pub/sub bus.

CAO's architecture in one rule: **no service calls another directly, the bus is
the sole broker** (§2.2). FifoReader publishes, LogWriter and StatusMonitor
subscribe, InboxService reacts to status — none of them holds a reference to
any other. That property is what lets the terminal plane, the hook plane and
the mailbox evolve independently, and it is worth protecting.

The one design decision here that is not obvious: **a slow subscriber must
never stall ingest.** Each subscriber owns a bounded queue, and when it
overflows the *oldest* event is dropped rather than the publisher blocking. A
UI that cannot keep up should fall behind and recover, not apply backpressure
all the way to a hook shim that is holding a worker's tool call open.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

#: Per-subscriber buffer. Deep enough to ride out a GC pause or a slow render,
#: shallow enough that a dead subscriber cannot hold megabytes hostage.
DEFAULT_QUEUE_SIZE = 1024


class Subscription:
    """One subscriber's view of the bus. Async-iterable."""

    def __init__(self, topic_prefix: str, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self.topic_prefix = topic_prefix
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._closed = False

    def matches(self, topic: str) -> bool:
        """Exact match, or a dot-delimited prefix.

        Delimiter-aware on purpose: subscribing to ``agent.claude`` must not
        also deliver ``agent.claude_2``'s traffic.
        """
        if not self.topic_prefix:
            return True
        return topic == self.topic_prefix or topic.startswith(self.topic_prefix + ".")

    def deliver(self, topic: str, event: Any) -> None:
        if self._closed:
            return
        try:
            self.queue.put_nowait((topic, event))
        except asyncio.QueueFull:
            # Drop the oldest, keep the newest: for live observability the most
            # recent state is what matters, and the durable copy is in SQLite
            # regardless. Counted so the gap is visible rather than silent.
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - race with a consumer
                pass
            try:
                self.queue.put_nowait((topic, event))
            except asyncio.QueueFull:  # pragma: no cover - race with a consumer
                self.dropped += 1

    def close(self) -> None:
        self._closed = True

    async def __aiter__(self) -> AsyncIterator[tuple[str, Any]]:
        while not self._closed:
            topic, event = await self.queue.get()
            yield topic, event


class EventBus:
    """Topic-routed fan-out. Publishing is synchronous and never awaits."""

    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []

    def subscribe(self, topic_prefix: str = "", maxsize: int = DEFAULT_QUEUE_SIZE) -> Subscription:
        sub = Subscription(topic_prefix, maxsize=maxsize)
        self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        subscription.close()
        try:
            self._subscriptions.remove(subscription)
        except ValueError:
            pass

    def publish(self, topic: str, event: Any) -> None:
        """Fan out to matching subscribers. Deliberately not a coroutine — the
        ingest path calls this and must not yield control to do so."""
        for sub in self._subscriptions:
            if sub.matches(topic):
                sub.deliver(topic, event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    @property
    def dropped_total(self) -> int:
        return sum(s.dropped for s in self._subscriptions)
