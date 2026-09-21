"""UUIDv7 and the W3C trace identifiers (§5.1)."""

import re
import uuid

from fleetview.ids import new_span_id, new_trace_id, uuid7


def test_version_and_variant_are_rfc_9562():
    value = uuid7()
    assert value.version == 7
    assert (value.int >> 62) & 0b11 == 0b10, "RFC 4122 variant bits"


def test_ids_sort_by_creation_time():
    """The whole reason for v7 over v4: an index on the id is an index on time,
    so events from three channels at three latencies still sort correctly."""
    ids = [str(uuid7()) for _ in range(500)]
    assert ids == sorted(ids)


def test_monotonic_within_a_single_millisecond():
    """500 ids generated back to back land in the same millisecond or two. They
    must still be strictly increasing, or the sort above is a coin flip under
    load — which is exactly when it matters."""
    ids = [uuid7().int for _ in range(500)]
    assert all(b > a for a, b in zip(ids, ids[1:]))


def test_ids_are_unique():
    assert len({uuid7() for _ in range(2000)}) == 2000


def test_timestamp_is_current():
    import time

    before = int(time.time() * 1000)
    value = uuid7().int >> 80
    after = int(time.time() * 1000)
    assert before <= value <= after


def test_parses_as_a_uuid():
    assert uuid.UUID(str(uuid7())).version == 7


def test_trace_and_span_id_shapes():
    """W3C Trace Context: 32 and 16 lowercase hex characters."""
    assert re.fullmatch(r"[0-9a-f]{32}", new_trace_id())
    assert re.fullmatch(r"[0-9a-f]{16}", new_span_id())
