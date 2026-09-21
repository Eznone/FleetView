"""The daemon's HTTP surface, served over a Unix socket.

§3.3 describes the hook shim as POSTing to a Unix domain socket, and that is
exactly what this is: an ordinary FastAPI app that uvicorn binds to
``~/.fleetview/daemon.sock`` rather than a TCP port. Nothing is listening on
the network, so no port needs defending — and the socket's file permissions are
the access control.

``POST /v1/events`` is both the hook plane's ingest and the reply channel for
``PreToolUse``. The response is a *policy* decision only: Phase 1 has no policy
engine, so it always answers "no opinion", and the CLI's own permission prompt
handles every case (§3.5.1).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.daemon.translate import translate
from fleetview.ids import new_event_id
from fleetview.store import EventStore

log = logging.getLogger(__name__)


def create_app(
    store: EventStore | None = None,
    *,
    bus: EventBus | None = None,
    settings: Settings | None = None,
    run_id: str | None = None,
    lifespan: Any = None,
) -> FastAPI:
    """Build the app.

    Handlers reach the store through ``request.app.state`` rather than closing
    over it, because the daemon opens the database in a **lifespan** (see
    ``server.py``) and therefore has no store to pass in at construction time.

    That indirection is load-bearing, not taste. uvicorn's ``capture_signals``
    re-raises SIGTERM to the default handler once ``serve()`` returns, so any
    cleanup written after ``await server.serve()`` never runs — the process is
    already gone. Shutdown work has to happen inside the lifespan, while
    uvicorn still owns the process. Symptom when this is wrong: queued events
    are lost and the SQLite WAL is never checkpointed, on every single stop.
    """
    app = FastAPI(title="FleetView daemon", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.bus = bus
    app.state.settings = settings or Settings()
    # One daemon process is one run, for now. Phase 3 scopes runs to a fleet
    # the operator actually started; until then this keeps sequences coherent.
    app.state.run_id = run_id or new_event_id()

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "runId": request.app.state.run_id,
            "events": await request.app.state.store.count(),
            "maxConcurrentAgents": request.app.state.settings.max_concurrent_active_agents,
        }

    @app.post("/v1/events")
    async def ingest(request: Request) -> dict[str, Any]:
        envelope = await request.json()

        event = translate(envelope, default_run_id=request.app.state.run_id)
        if event is None:
            # An unrecognised hook type. A CLI update must not be able to make
            # ingest fail; log it and move on.
            log.info("ignoring unknown hook: %r", (envelope.get("hook") or {}).get("hook_event_name"))
            return _no_opinion()

        request.app.state.store.append(event)
        return _no_opinion()

    return app


def _no_opinion() -> dict[str, Any]:
    """FleetView's policy layer has nothing to say about this call.

    Deliberately NOT ``{"permissionDecision": "allow"}``. An allow would
    suppress the CLI's own permission prompt and leave the fleet running with
    the vendor guardrail switched off — the full-bypass posture §3.5.1 rejects.
    The policy engine that can answer "deny" arrives in Phase 5; until then the
    honest answer is silence.
    """
    return {"permissionDecision": None}
