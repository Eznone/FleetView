"""The browser's surface: a second listener, and a deliberately smaller one.

Phase 1 could say that nothing was listening on the network and that the
socket's file permissions *were* the access control (:mod:`fleetview.daemon.app`).
A browser cannot dial an AF_UNIX socket, so Phase 2 has to give that up. What
replaces it is a split rather than a weakening:

- The hook plane keeps the Unix socket, unchanged, and keeps every mutating
  route. Ingest, tap attach and tap detach are not reachable over TCP at all.
- This app is **read-only**, and `test_no_mutating_route_is_reachable_over_tcp`
  enforces that by walking the route table rather than trusting review.
- The bind is loopback, and refused otherwise (`check_ui_host`) rather than
  merely defaulted -- because the terminal plane serves pane bytes verbatim and
  unredacted until Phase 6, and those can contain a sign-in URL an agent echoed.

Loopback is not on its own access control: any page the operator visits can
resolve a name to 127.0.0.1 and reach this port. So `LoopbackGuard` validates
`Host` on every request and `Origin` on every upgrade. The Phase 5 control
plane (`/ws/control`) is a separate decision, and this file is where it will
have to be argued for rather than something this phase leaks in.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, State

from fleetview.config import Settings
from fleetview.projection import PROJECTION_TOPIC
from fleetview.terminal.paths import (
    UnsafeAgentIdError,
    agent_log_dir,
    agent_slug,
    list_segments,
    read_from,
    seek_back,
)
from fleetview.terminal.writer import output_topic

log = logging.getLogger(__name__)

#: How often a pane WebSocket looks for new bytes.
#:
#: The pane follows the *file*, not the bus, and that is a decision rather than
#: an oversight. The writer is itself a bus subscriber, so the file lags the
#: bus by an unbounded-in-principle amount -- which means no ordering of
#: "subscribe, then replay from disk" can close the seam between them: bytes
#: published just before the subscribe may not be on disk when the replay reads
#: it, and they are in neither stream. Following one source has no seam at all,
#: reuses the read path `fleetview terminal tail` already proves, and keeps the
#: file authoritative exactly as §2.5 intends. The cost is this much latency on
#: pane bytes, which no gate measures -- §7's <100 ms budget is node *state*.
TERMINAL_POLL_MS = 100

#: Nothing is served from here without a Host header in this set.
def _allowed_hosts(host: str, port: int) -> set[str]:
    names = {host, "localhost", "127.0.0.1", "[::1]"}
    return {n.lower() for n in names} | {f"{n}:{port}".lower() for n in names}


class UiHostNotLoopbackError(RuntimeError):
    """Raised when the UI would bind somewhere a browser is not the only reader."""


def check_ui_host(host: str) -> None:
    """Refuse any bind address that is not loopback.

    Same spirit as the 9p refusal and the socket-length check: fail at startup,
    naming the setting that caused it, rather than working until the day it
    matters. What is at stake is specific -- `GET /v1/terminals/{id}/history`
    and `/ws/terminal/{id}` serve an agent's screen verbatim, and
    :mod:`fleetview.config` records that a screen can contain a sign-in URL or
    a token the agent echoed back. On 0.0.0.0 that is served to the network.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise UiHostNotLoopbackError(
            f"refusing to bind the FleetView UI to {host!r}: it is not a loopback address.\n"
            "The UI serves captured terminal bytes verbatim and unredacted "
            "(redaction is Phase 6), which can include a sign-in URL or a token "
            "an agent echoed to its screen.\n"
            "Set FLEETVIEW_UI_HOST to 127.0.0.1, or FLEETVIEW_UI=0 to run headless."
        )


class UiPortInUseError(RuntimeError):
    """Raised when the UI's TCP port is already taken."""


def check_ui_port(host: str, port: int) -> None:
    """Refuse a port that is already bound, before uvicorn tries it.

    Left alone this surfaces as ``OSError: [Errno 98] address already in use``
    from inside uvicorn's startup, under a traceback that never mentions
    FleetView's own setting -- while the overwhelmingly likely cause is the
    benign one: a second daemon, or one the operator forgot was running. Same
    posture as the socket-length refusal (`PHASE_1.md` F10): fail early, name
    the setting, say what to do.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    except OSError as exc:
        raise UiPortInUseError(
            f"cannot bind the FleetView UI to {host}:{port} -- {exc.strerror}.\n"
            "Another FleetView daemon is probably already running.\n"
            "Set FLEETVIEW_UI_PORT to a free port, or FLEETVIEW_UI=0 to run headless."
        ) from exc
    finally:
        probe.close()


class LoopbackGuard:
    """Host and Origin validation, as raw ASGI so it covers upgrades too.

    Starlette's `BaseHTTPMiddleware` only sees `http` scopes, and the two
    WebSockets are the routes with the most to lose, so this is written at the
    ASGI layer instead.

    An absent `Origin` is allowed on purpose: a browser always sends one on a
    WebSocket handshake and cannot be made not to, so absence means a
    non-browser client -- `curl`, a test, a future CLI -- which the `Host`
    check already covers.
    """

    def __init__(self, app: Any, *, hosts: set[str]) -> None:
        self.app = app
        self._hosts = hosts

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = Headers(scope=scope)
            host = headers.get("host", "").split(",")[0].strip().lower()
            if host not in self._hosts:
                await self._reject(scope, send, f"unexpected Host header: {host!r}")
                return
            origin = headers.get("origin")
            if origin is not None:
                name = origin.split("//", 1)[-1].strip().lower()
                if name not in self._hosts:
                    await self._reject(scope, send, f"unexpected Origin: {origin!r}")
                    return
        await self.app(scope, receive, send)

    async def _reject(self, scope, send, reason: str) -> None:
        log.warning("refused a request to the UI listener: %s", reason)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b"FleetView serves 127.0.0.1 only.\n"
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


class WebSocketRegistry:
    """Every live UI socket, so shutdown can close them deliberately.

    Ordering matters at teardown: these close *before* the terminal supervisor
    stops, so a connected pane sees `terminal.tap.closed` arrive rather than
    its socket vanishing underneath it. See the lifespan in
    :mod:`fleetview.daemon.server`.
    """

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()

    def add(self, socket: WebSocket) -> None:
        self._sockets.add(socket)

    def discard(self, socket: WebSocket) -> None:
        self._sockets.discard(socket)

    @property
    def count(self) -> int:
        return len(self._sockets)

    async def close_all(self) -> None:
        for socket in list(self._sockets):
            with contextlib.suppress(Exception):
                await socket.close(code=1001)
        self._sockets.clear()


def find_ui_dist() -> Path | None:
    """Where the built SPA lives, or None if it has not been built.

    `FLEETVIEW_UI_DIST` wins, then a bundled build inside the package, then the
    working tree's `ui/dist`. A missing build is answered with a page naming
    the command to run -- a silent 404 is the failure shape this project keeps
    finding, and it is not worth repeating on the first screen a new operator
    ever sees.
    """
    override = os.environ.get("FLEETVIEW_UI_DIST")
    if override:
        candidate = Path(override).expanduser()
        return candidate if (candidate / "index.html").is_file() else None
    for candidate in (
        Path(__file__).resolve().parents[1] / "ui_dist",
        Path(__file__).resolve().parents[3] / "ui" / "dist",
    ):
        if (candidate / "index.html").is_file():
            return candidate
    return None


_NO_BUILD_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>FleetView</title>
<style>body{font:15px/1.6 ui-monospace,monospace;margin:4rem auto;max-width:44rem;
padding:0 1.5rem;background:#0f1115;color:#d8dee9}code{background:#1c2029;padding:.15em .4em;
border-radius:4px}h1{font-size:1.3rem}</style></head><body>
<h1>FleetView &mdash; the UI is not built yet</h1>
<p>The daemon is running and its read API is live, but the single-page app has
not been compiled. From the repository root:</p>
<p><code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>Then reload this page. The API is available meanwhile at
<code>/v1/agents</code>, <code>/v1/events</code> and <code>/v1/health</code>.</p>
</body></html>
"""


def create_ui_app(
    *, settings: Settings, source: State, lifespan: Any = None
) -> FastAPI:
    """Build the read-only UI app.

    ``source`` is the hook app's ``app.state``, not a bag of resources copied
    out of it. The daemon opens everything in a lifespan on *that* app, and the
    existing tests inject fakes by assigning to it after construction; reading
    through the same object means both keep working and neither can drift.

    ``lifespan`` is for tests. The daemon runs its lifespan on the *hook* app,
    which owns every resource; a test driving this app through Starlette's
    ``TestClient`` needs those resources created on the portal's loop instead,
    because an aiosqlite connection opened on one loop cannot be awaited from
    another.
    """
    app = FastAPI(
        title="FleetView UI", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.source = source
    app.state.settings = settings
    app.state.clients = WebSocketRegistry()
    app.add_middleware(LoopbackGuard, hosts=_allowed_hosts(settings.ui_host, settings.ui_port))

    def _state(request: Request) -> State:
        return request.app.state.source

    def _slug(agent_id: str) -> str:
        try:
            return agent_slug(agent_id)
        except UnsafeAgentIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- read API ----------------------------------------------------------

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        state = _state(request)
        fleet = getattr(state, "projection", None)
        return {
            "status": "ok",
            "runId": state.run_id,
            "events": await state.store.count(),
            "maxConcurrentAgents": request.app.state.settings.max_concurrent_active_agents,
            "pending": state.store.pending,
            "terminals": state.terminals.count,
            "agents": fleet.fleet.agent_count if fleet else 0,
            "uiClients": request.app.state.clients.count,
        }

    @app.get("/v1/agents")
    async def agents(request: Request) -> dict[str, Any]:
        """The canvas, from memory. §4.1 tier 3: the DB is never the render path."""
        fleet = getattr(_state(request), "projection", None)
        if fleet is None:
            raise HTTPException(status_code=503, detail="projection is not running")
        return fleet.fleet.snapshot().model_dump(by_alias=True, mode="json")

    @app.get("/v1/events")
    async def events(
        request: Request,
        run: str | None = Query(None),
        agent: str | None = Query(None),
        type: str | None = Query(None),
        grep: str | None = Query(None),
        after_id: str | None = Query(None, alias="afterId"),
        limit: int = Query(200, ge=1, le=5000),
        newest: bool = Query(True),
    ) -> list[dict[str, Any]]:
        found = await _state(request).store.fetch(
            run_id=run, agent_id=agent, event_type=type, grep=grep,
            after_id=after_id, limit=limit, newest=newest,
        )
        return [event.to_wire() for event in found]

    @app.get("/v1/terminals")
    async def terminals(request: Request) -> list[dict[str, Any]]:
        return _state(request).terminals.status()

    @app.get("/v1/terminals/{agent_id}/history")
    async def history(
        request: Request,
        agent_id: str,
        bytes_: int = Query(65536, alias="bytes", ge=0, le=8 * 1024 * 1024),
        since: str | None = Query(None),
    ) -> Response:
        """Raw ANSI, for xterm.js to replay.

        Deliberately not stripped: the CLI's escape-sequence filter exists so a
        human can grep a log, and handing a terminal emulator the same output
        would strip exactly the bytes it is built to render.
        """
        agent = _slug(agent_id)
        state = _state(request)
        segments = list_segments(agent_log_dir(request.app.state.settings, agent))
        if not segments:
            return Response(content=b"", media_type="application/octet-stream")

        if since is not None and state.chunks is not None:
            try:
                start = datetime.fromisoformat(since)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"bad since: {since!r}") from exc
            rows = await state.chunks.fetch(agent, since=start, limit=1)
            position = (Path(rows[0].path), rows[0].byte_offset) if rows else (segments[0], 0)
        else:
            position = seek_back(segments, bytes_)

        payload = await asyncio.to_thread(read_from, segments, position[0], position[1])
        return Response(content=payload, media_type="application/octet-stream")

    # --- the live feed -----------------------------------------------------

    @app.websocket("/ws/live")
    async def live(websocket: WebSocket) -> None:
        state = _state(websocket)
        bus = state.bus
        fleet = getattr(state, "projection", None)
        if bus is None or fleet is None:
            await websocket.close(code=1011)
            return

        await websocket.accept()
        registry = websocket.app.state.clients
        registry.add(websocket)
        depth = websocket.app.state.settings.ui_bus_queue_size

        events_sub = bus.subscribe("events", maxsize=depth)
        fleet_sub = bus.subscribe("projection", maxsize=depth)
        sink: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=depth)
        tasks = [
            asyncio.create_task(_pump(events_sub, sink, _wrap_event)),
            asyncio.create_task(_pump(fleet_sub, sink, _wrap_snapshot)),
        ]
        try:
            await websocket.send_json(_wrap_snapshot(fleet.fleet.snapshot()))
            while True:
                await websocket.send_json(await sink.get())
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass
        finally:
            # `Subscription.close()` only sets a flag; it does not wake a task
            # blocked in `queue.get()`. Unsubscribing without cancelling would
            # leave both pumps alive forever, and every leaked subscription is
            # permanent work on the ingest fan-out.
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            bus.unsubscribe(events_sub)
            bus.unsubscribe(fleet_sub)
            registry.discard(websocket)

    @app.websocket("/ws/terminal/{agent_id}")
    async def terminal(websocket: WebSocket, agent_id: str) -> None:
        try:
            agent = agent_slug(agent_id)
        except UnsafeAgentIdError:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        registry = websocket.app.state.clients
        registry.add(websocket)
        settings = websocket.app.state.settings
        directory = agent_log_dir(settings, agent)
        try:
            segments = await asyncio.to_thread(list_segments, directory)
            if segments:
                start, offset = seek_back(segments, 65536)
                backlog = await asyncio.to_thread(read_from, segments, start, offset)
                if backlog:
                    await websocket.send_bytes(backlog)
                current, cursor = segments[-1], segments[-1].stat().st_size
            else:
                current, cursor = None, 0

            while True:
                await asyncio.sleep(TERMINAL_POLL_MS / 1000.0)
                live_segments = await asyncio.to_thread(list_segments, directory)
                if not live_segments:
                    continue
                if current is None:
                    current, cursor = live_segments[0], 0
                if live_segments[-1] != current:
                    # Re-listed every pass because the writer may have rotated
                    # underneath us; following only the file we opened would go
                    # quiet at the rotation while the agent kept talking.
                    tail = await asyncio.to_thread(_read_at, current, cursor)
                    if tail:
                        await websocket.send_bytes(tail)
                    current, cursor = live_segments[-1], 0
                size = current.stat().st_size
                if size > cursor:
                    chunk = await asyncio.to_thread(_read_at, current, cursor)
                    if chunk:
                        await websocket.send_bytes(chunk)
                    cursor = size
        except (WebSocketDisconnect, RuntimeError, ConnectionError, OSError):
            pass
        finally:
            registry.discard(websocket)

    # --- the SPA, mounted last so it cannot shadow the API -----------------

    dist = find_ui_dist()
    if dist is not None:
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
        log.info("serving the FleetView UI from %s", dist)
    else:
        @app.get("/", response_class=HTMLResponse)
        async def no_build() -> str:
            return _NO_BUILD_PAGE

    return app


def _read_at(path: Path, offset: int) -> bytes:
    with open(path, "rb") as handle:
        handle.seek(offset)
        return handle.read()


def _wrap_event(item: Any) -> dict[str, Any]:
    return {"type": "event", "event": item.to_wire()}


def _wrap_snapshot(item: Any) -> dict[str, Any]:
    return {"type": "snapshot", "snapshot": item.model_dump(by_alias=True, mode="json")}


async def _pump(subscription, sink: asyncio.Queue, wrap) -> None:
    """Merge one subscription into the socket's single send queue.

    Blocking on a full ``sink`` is the intended backpressure: it stalls this
    pump, the subscription's own queue then fills, and the *bus* drops its
    oldest item. That is the designed failure mode -- a UI that cannot keep up
    falls behind and recovers, and never pushes back toward a hook shim.
    """
    while True:
        _topic, item = await subscription.queue.get()
        with contextlib.suppress(Exception):
            await sink.put(wrap(item))


__all__ = [
    "PROJECTION_TOPIC",
    "LoopbackGuard",
    "UiHostNotLoopbackError",
    "UiPortInUseError",
    "WebSocketRegistry",
    "check_ui_host",
    "check_ui_port",
    "create_ui_app",
    "find_ui_dist",
    "output_topic",
]
