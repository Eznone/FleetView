"""Starting and stopping the daemon."""

from __future__ import annotations

import contextlib
import logging
import socket
from pathlib import Path

import uvicorn

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.daemon.app import create_app
from fleetview.fsguard import require_fast_filesystem
from fleetview.store import EventStore, apply_schema, connect

log = logging.getLogger(__name__)


def clear_stale_socket(path: Path) -> bool:
    """Remove a socket file left behind by a daemon that did not shut down.

    A Unix socket is a file, and an unclean exit leaves it there. Binding then
    fails with "address already in use" — which reads as "a daemon is already
    running" and sends the operator hunting for a process that does not exist.
    So: try to connect. If nothing answers, the file is debris and we remove it.
    If something does answer, leave it alone and let the bind fail honestly.
    """
    if not path.exists():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            path.unlink(missing_ok=True)
            return True
    return False


async def serve(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()

    # Before anything is created: §4.1's refusal. The error names the reason,
    # because the alternative is intermittent lock corruption weeks later.
    # The path need not exist yet — the guard resolves it either way — so the
    # error names the directory the operator actually asked for.
    require_fast_filesystem(settings.home)
    settings.ensure_directories()

    if clear_stale_socket(settings.socket_path):
        log.warning("removed stale socket at %s", settings.socket_path)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        """Own every resource for exactly as long as the server runs.

        This must be a lifespan rather than a try/finally around
        ``server.serve()``. uvicorn's ``capture_signals`` re-raises SIGTERM to
        the default handler after ``serve()`` returns, which terminates the
        process — so code after that await never runs. Putting shutdown here
        means it happens while uvicorn still owns the process.

        What is lost when this is wrong is quiet and real: events still sitting
        in the writer's queue, and a SQLite WAL that is never checkpointed.
        """
        conn = await connect(settings.db_path)
        await apply_schema(conn)

        bus = EventBus()
        store = EventStore(conn, bus=bus, settings=settings)
        await store.start()

        app.state.store = store
        app.state.bus = bus

        try:
            yield
        finally:
            await store.stop()      # drains the queue; never drops buffered events
            await conn.close()      # checkpoints and removes the -wal/-shm files
            with contextlib.suppress(OSError):
                settings.socket_path.unlink(missing_ok=True)

    app = create_app(settings=settings, lifespan=lifespan)
    config = uvicorn.Config(
        app,
        uds=str(settings.socket_path),
        log_level="warning",
        access_log=False,
    )
    await uvicorn.Server(config).serve()
