"""Shared setup for the UI listener's tests.

Not a conftest: this repo keeps fixtures inline and beside the file that uses
them, and four test modules needing the same six-line wiring is not a reason to
introduce implicit collection.

Why the resources are built in a **lifespan on the UI app** rather than in an
async fixture, as most of this suite does: Starlette's ``TestClient`` is the
only client that speaks WebSockets, and it runs the app on a portal thread with
its own event loop. An aiosqlite connection opened on the test's loop cannot be
awaited from the portal's, so anything the routes touch has to be created
there. That makes these tests synchronous, unlike their neighbours.
"""

from __future__ import annotations

import contextlib

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.daemon.app import create_app
from fleetview.daemon.ui_app import create_ui_app
from fleetview.projection import ProjectionService
from fleetview.store import EventStore, TerminalChunkStore, apply_schema, connect

#: TestClient defaults to `Host: testserver`, which LoopbackGuard rejects --
#: correctly. Every client here dials the address an operator's browser would.
BASE_URL = "http://127.0.0.1:8420"

RUN = "run-1"


def build(tmp_path, **overrides):
    """Return ``(ui_app, hook_app, settings)``, wired as the daemon wires them."""
    settings = Settings(home=tmp_path, flush_interval_ms=10, **overrides)
    settings.ensure_directories()
    hook = create_app(settings=settings, run_id=RUN)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        conn = await connect(settings.db_path)
        await apply_schema(conn)
        bus = EventBus()
        store = EventStore(conn, bus=bus, settings=settings)
        await store.start()
        chunks = TerminalChunkStore(conn, settings=settings)
        await chunks.start()
        projection = ProjectionService(
            settings=settings, bus=bus, store=store, run_id=RUN
        )
        await projection.start()

        hook.state.store = store
        hook.state.bus = bus
        hook.state.chunks = chunks
        hook.state.projection = projection
        try:
            yield
        finally:
            await app.state.clients.close_all()
            await projection.stop()
            await store.stop()
            await chunks.stop()
            await conn.close()

    ui = create_ui_app(settings=settings, source=hook.state, lifespan=lifespan)
    return ui, hook, settings


async def emit(store, event) -> None:
    """Append and commit, so the bus fan-out has actually happened."""
    store.append(event)
    await store.flush()


def ws(client, path: str, **kwargs):
    """Open a WebSocket with a Host the LoopbackGuard will accept.

    Starlette's ``TestClient.websocket_connect`` hardcodes
    ``urljoin("ws://testserver", url)`` and ignores ``base_url`` entirely, so
    every upgrade would otherwise arrive claiming ``Host: testserver`` and be
    refused. Set here rather than per test, so the guard stays asserted in
    `test_ui_security` instead of quietly disabled everywhere.
    """
    headers = {"Host": "127.0.0.1:8420", **kwargs.pop("headers", {})}
    return client.websocket_connect(path, headers=headers, **kwargs)
