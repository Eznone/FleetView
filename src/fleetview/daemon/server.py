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
from fleetview.store import EventStore, TerminalChunkStore, apply_schema, connect
from fleetview.terminal.supervisor import NullTerminalSupervisor, TerminalSupervisor

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


#: ``sockaddr_un.sun_path`` is 108 bytes including the terminating NUL, so a
#: socket path may be at most 107. This is a kernel ABI constant, not a policy.
MAX_SOCKET_PATH_BYTES = 107


def check_socket_path(path: Path) -> None:
    """Refuse a socket path the kernel cannot bind, with a usable message.

    Left to itself this surfaces as ``OSError: AF_UNIX path too long`` from
    deep inside uvicorn's startup, under a traceback that names neither the
    path nor ``FLEETVIEW_HOME`` — which is the setting that actually caused it.
    Same reasoning as the 9p refusal below: fail early and say why.
    """
    length = len(str(path).encode())
    if length > MAX_SOCKET_PATH_BYTES:
        raise SocketPathTooLongError(
            f"socket path is {length} bytes, over the kernel's "
            f"{MAX_SOCKET_PATH_BYTES}-byte limit for a Unix socket:\n"
            f"  {path}\n"
            f"Set FLEETVIEW_HOME to a shorter path (default: ~/.fleetview)."
        )


class SocketPathTooLongError(RuntimeError):
    """Raised when FLEETVIEW_HOME makes the daemon socket unbindable."""


async def serve(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()

    # Before anything is created: §4.1's refusal. The error names the reason,
    # because the alternative is intermittent lock corruption weeks later.
    # The path need not exist yet — the guard resolves it either way — so the
    # error names the directory the operator actually asked for.
    require_fast_filesystem(settings.home)
    check_socket_path(settings.socket_path)
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

        chunks = TerminalChunkStore(conn, settings=settings)
        await chunks.start()

        if settings.terminal_capture_enabled:
            terminals = TerminalSupervisor(
                settings, chunks=chunks, events=store, bus=bus,
                run_id=app.state.run_id,
            )
        else:
            terminals = NullTerminalSupervisor()
        await terminals.start()

        app.state.store = store
        app.state.bus = bus
        app.state.chunks = chunks
        app.state.terminals = terminals

        try:
            yield
        finally:
            # This order is load-bearing, and each step pays for the one before.
            #
            # Terminals first: tearing a tap down detaches the pipe, drains the
            # FIFO, indexes the final byte range and emits tap-closed. Stopping
            # the event store before this would drop those events on the floor,
            # and stopping the chunk store first would lose the final index row
            # -- a byte range nothing can ever find again, because the segment
            # file carries no index of its own.
            await terminals.stop()
            await store.stop()      # drains the queue; never drops buffered events
            await chunks.stop()     # ...and neither does this one
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
