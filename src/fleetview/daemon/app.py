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

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.daemon.translate import translate
from fleetview.ids import new_event_id
from fleetview.spawn.tmux import TerminalTapError
from fleetview.terminal.supervisor import NullTerminalSupervisor
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
    # Replaced by the lifespan when the daemon runs for real. Defaulted here so
    # an app built directly — which the tests do, and which has no FIFOs to own
    # — still answers every route instead of raising AttributeError.
    app.state.chunks = None
    app.state.terminals = NullTerminalSupervisor()
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
            # `pending` is the load gate's stall signal: a queue that grows
            # monotonically is what "the daemon is not keeping up" looks like.
            "pending": request.app.state.store.pending,
            "terminals": request.app.state.terminals.count,
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

    # --- the terminal plane (channel B) ------------------------------------
    #
    # The daemon owns the FIFO, so it owns the tap. Spawning still lives in the
    # CLI: this route is the fast path so `fleetview spike` does not wait a
    # reconcile interval, and tmux remains the registry either way.

    @app.post("/v1/terminals", status_code=201)
    async def attach_terminal(request: Request, body: AttachRequest) -> dict[str, Any]:
        try:
            handle = await request.app.state.terminals.attach(
                body.agent_id, body.tmux_session
            )
        except TerminalTapError as exc:
            # "no such session" is the caller's mistake; a tap that would not
            # verify is a conflict with the live state of the pane.
            status = 404 if "no tmux session" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return {
            "agentId": handle.agent_id,
            "tmuxSession": handle.tmux_session,
            "fifo": str(handle.fifo),
            "attachedAt": handle.attached_at.isoformat(),
        }

    @app.delete("/v1/terminals/{agent_id}")
    async def detach_terminal(request: Request, agent_id: str) -> dict[str, Any]:
        return {"detached": await request.app.state.terminals.detach(agent_id)}

    @app.get("/v1/terminals")
    async def list_terminals(request: Request) -> list[dict[str, Any]]:
        return request.app.state.terminals.status()

    return app


class AttachRequest(BaseModel):
    """Body of POST /v1/terminals.

    camelCase on the wire and ``extra="forbid"``, matching
    :mod:`fleetview.schema.events` — a typo'd field should be a 422, not a
    silently ignored key that leaves the caller thinking it asked for something
    it did not.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    agent_id: str
    tmux_session: str


def _no_opinion() -> dict[str, Any]:
    """FleetView's policy layer has nothing to say about this call.

    Deliberately NOT ``{"permissionDecision": "allow"}``. An allow would
    suppress the CLI's own permission prompt and leave the fleet running with
    the vendor guardrail switched off — the full-bypass posture §3.5.1 rejects.
    The policy engine that can answer "deny" arrives in Phase 5; until then the
    honest answer is silence.
    """
    return {"permissionDecision": None}
