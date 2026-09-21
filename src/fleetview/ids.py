"""Time-ordered identifiers.

PROJECT_PLAN.md §5.1 specifies UUIDv7 for event ids. The stdlib gained
``uuid.uuid7`` in Python 3.14 and this project targets 3.12, so it is
implemented here rather than pulled in as a dependency — it is thirty lines and
a dependency on the ingest path is not worth it.

Why v7 and not v4: the id sorts by creation time, so an index on it is an index
on time, and events arriving out of order from three different channels (§3.4)
still sort correctly without consulting the timestamp column.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = -1
_counter = 0

#: rand_a is 12 bits, so this is its ceiling. Reaching it means 4096 ids inside
#: a single millisecond, at which point we wait for the clock rather than
#: risking a collision or a non-monotonic id.
_COUNTER_MAX = 0xFFF


def uuid7() -> uuid.UUID:
    """A UUIDv7: 48-bit millisecond timestamp, then a counter, then randomness.

    Monotonic within a process even when several ids are created in the same
    millisecond — the 12-bit ``rand_a`` field is used as a sub-millisecond
    counter, which is the method RFC 9562 §6.2 calls "replace leftmost random
    bits with increased clock precision".
    """
    global _last_ms, _counter

    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms = now_ms
            _counter = secrets.randbelow(_COUNTER_MAX // 2)  # headroom to count up into
        else:
            # Clock has not advanced (or went backwards — same handling).
            _counter += 1
            if _counter > _COUNTER_MAX:
                while now_ms <= _last_ms:
                    time.sleep(0.0002)
                    now_ms = time.time_ns() // 1_000_000
                _last_ms = now_ms
                _counter = 0
            now_ms = _last_ms
        timestamp, counter = now_ms, _counter

    value = (timestamp & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76                      # version 7
    value |= (counter & _COUNTER_MAX) << 64
    value |= 0b10 << 62                     # RFC 4122 variant
    value |= int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    return uuid.UUID(int=value)


def new_event_id() -> str:
    return str(uuid7())


def new_trace_id() -> str:
    """W3C Trace Context trace-id: 32 lowercase hex characters."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """W3C Trace Context span-id: 16 lowercase hex characters."""
    return secrets.token_hex(8)
