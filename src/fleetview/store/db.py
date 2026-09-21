"""Opening the database with the PRAGMAs §4.1 settled on.

Every connection gets these. A connection opened without them is not a
performance problem, it is a correctness one — ``foreign_keys`` is off by
default in SQLite, and WAL is a property of the *database file* that a
non-WAL connection will happily fight with.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from fleetview.fsguard import require_fast_filesystem

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

#: §4.1. `synchronous=NORMAL` can lose the last few milliseconds of telemetry
#: on power loss — but not on application crash, since the WAL is still
#: written. That is the right trade for observability data.
#:
#: The mailbox will need `synchronous=FULL` when it lands in Phase 3: a message
#: marked "delivered" that then evaporates is a correctness bug, not a lost
#: metric.
PRAGMAS = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "5000"),
    ("foreign_keys", "ON"),
)


async def connect(db_path: Path, *, check_filesystem: bool = True) -> aiosqlite.Connection:
    """Open a connection with FleetView's PRAGMAs applied.

    The filesystem check happens here rather than in the daemon's startup so
    that no code path can reach a database on 9p by going around it.
    """
    if check_filesystem:
        require_fast_filesystem(db_path.parent)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    for pragma, value in PRAGMAS:
        await conn.execute(f"PRAGMA {pragma}={value}")
    await conn.commit()
    return conn


async def apply_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA_PATH.read_text())
    await conn.commit()
