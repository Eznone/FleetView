-- FleetView event store (PROJECT_PLAN.md §4.1).
--
-- TIER 1 lives here: structured, append-only, ~75 events/s at peak, replayable.
--
-- TIER 2 DOES NOT. Raw terminal bytes are 50-500 KB/min per agent -- a 15-agent
-- day is multiple gigabytes -- and §4.1 names row-per-chunk in SQLite as *the*
-- scaling mistake this schema invites. The bytes go to flat append-only files;
-- the database stores only where to find them. There is deliberately no BLOB
-- column anywhere in this file, and a test asserts that.

CREATE TABLE IF NOT EXISTS events (
    id                TEXT    PRIMARY KEY,   -- UUIDv7: sorts by creation time
    run_id            TEXT    NOT NULL,
    sequence          INTEGER NOT NULL,      -- monotonic per run, writer-assigned
    timestamp         TEXT    NOT NULL,      -- ISO-8601 UTC, microsecond precision
    trace_id          TEXT    NOT NULL,      -- W3C Trace Context, 32 hex
    span_id           TEXT    NOT NULL,      -- W3C Trace Context, 16 hex
    parent_span_id    TEXT,
    agent_id          TEXT,
    channel           TEXT    NOT NULL,      -- hook | terminal | transcript | daemon
    event_type        TEXT    NOT NULL,
    payload           TEXT    NOT NULL,      -- JSON
    provider_metadata TEXT,                  -- JSON, namespaced raw

    -- Ordering is a correctness property, not a convenience: replay rebuilds
    -- every projection from this log, so a duplicated sequence within a run
    -- would make the rebuild ambiguous.
    UNIQUE (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events (run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_agent_time   ON events (agent_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type         ON events (event_type);

-- TIER 2 index. {path, byte_offset, length} and nothing else.
-- "offset" is a reserved word in SQL, hence byte_offset.
CREATE TABLE IF NOT EXISTS terminal_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT    NOT NULL,
    path        TEXT    NOT NULL,   -- flat file on disk, written by tmux pipe-pane
    byte_offset INTEGER NOT NULL,
    length      INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_terminal_chunks_agent ON terminal_chunks (agent_id, id);
