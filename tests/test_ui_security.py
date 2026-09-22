"""The compensating controls for Phase 2's one genuine loss.

Phase 1 could say that nothing was listening on the network and that the
socket's file permissions *were* the access control. A browser cannot dial a
Unix socket, so Phase 2 gives that up. These tests are what replaces it, and
they are the reason the split is safe rather than merely intended.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from fleetview.config import Settings
from fleetview.daemon.ui_app import UiHostNotLoopbackError, check_ui_host
from uiharness import BASE_URL, build


# --- the read-only invariant --------------------------------------------------


def test_no_mutating_route_is_reachable_over_tcp(tmp_path):
    """The whole justification for two listeners, asserted rather than reviewed.

    Ingest, tap attach and tap detach stay on the Unix socket, where file
    permissions are still the access control. If a POST ever appears here, the
    argument in `ui_app`'s docstring has stopped being true and Phase 5's
    control plane has leaked in early.
    """
    ui, _hook, _settings = build(tmp_path)
    methods: set[str] = set()
    for route in ui.routes:
        methods |= set(getattr(route, "methods", None) or set())
    assert methods <= {"GET", "HEAD"}, f"the UI listener exposes {methods - {'GET', 'HEAD'}}"


def test_the_hook_plane_still_owns_every_mutating_route(tmp_path):
    _ui, hook, _settings = build(tmp_path)
    mutating = {
        (route.path, method)
        for route in hook.routes
        for method in (getattr(route, "methods", None) or set())
        if method in {"POST", "DELETE", "PUT", "PATCH"}
    }
    assert ("/v1/events", "POST") in mutating
    assert ("/v1/terminals", "POST") in mutating
    assert ("/v1/terminals/{agent_id}", "DELETE") in mutating


# --- the bind -----------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_a_non_loopback_bind_is_refused(host):
    """Captured pane bytes are unredacted until Phase 6.

    A screen can hold a sign-in URL or a token an agent echoed, so binding
    beyond loopback serves those to the network. Refused at startup, naming the
    setting -- the same posture as the 9p refusal.
    """
    with pytest.raises(UiHostNotLoopbackError) as caught:
        check_ui_host(host)
    assert "FLEETVIEW_UI_HOST" in str(caught.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_loopback_is_allowed(host):
    check_ui_host(host)


# --- DNS rebinding ------------------------------------------------------------


def test_a_foreign_host_header_is_refused(tmp_path):
    """Loopback is not on its own access control.

    Any page the operator visits can resolve a name it controls to 127.0.0.1
    and reach this port from the browser. The Host header is what distinguishes
    that from the operator's own tab.
    """
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        response = client.get("/v1/health", headers={"Host": "fleetview.attacker.example"})
        assert response.status_code == 403
        assert b"127.0.0.1" in response.content


def test_a_foreign_origin_is_refused(tmp_path):
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        response = client.get("/v1/health", headers={"Origin": "https://attacker.example"})
        assert response.status_code == 403


def test_the_operators_own_tab_is_allowed(tmp_path):
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        response = client.get("/v1/health", headers={"Origin": BASE_URL})
        assert response.status_code == 200


def test_an_absent_origin_is_allowed(tmp_path):
    """A browser always sends Origin on an upgrade and cannot be made not to.

    Absence therefore means a non-browser client -- curl, a test, a future CLI
    -- which the Host check already covers.
    """
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        assert client.get("/v1/health").status_code == 200


def test_a_websocket_from_a_foreign_host_is_refused(tmp_path):
    """The guard is raw ASGI precisely so it covers upgrades.

    Starlette's BaseHTTPMiddleware only sees `http` scopes, and the two
    WebSockets are the routes with the most to lose.
    """
    from starlette.websockets import WebSocketDisconnect

    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(
                "/ws/live", headers={"Host": "fleetview.attacker.example"}
            ):
                pass
        assert caught.value.code == 1008, "policy violation, not a normal close"


# --- path traversal -----------------------------------------------------------


@pytest.mark.parametrize("agent", ["../../etc/passwd", "a/b", "..", "with space"])
def test_an_unsafe_agent_id_never_reaches_a_path(tmp_path, agent):
    """`agent_slug` is the one validator, and it raises rather than rewrites."""
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        response = client.get(f"/v1/terminals/{agent}/history")
        assert response.status_code in (400, 404)


def test_the_settings_default_is_loopback():
    assert Settings().ui_host == "127.0.0.1"


# --- the port -----------------------------------------------------------------


def test_a_port_already_in_use_is_refused_with_a_usable_message():
    """Otherwise this is an uvicorn traceback that never names FleetView.

    The likely cause is entirely benign -- a second daemon, or one the
    operator forgot was running -- so the message says that rather than
    leaving them to read a bind error. Same posture as the socket-length
    refusal (`PHASE_1.md` F10).
    """
    import socket as socketlib

    from fleetview.daemon.ui_app import UiPortInUseError, check_ui_port

    holder = socketlib.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(UiPortInUseError) as caught:
            check_ui_port("127.0.0.1", port)
    finally:
        holder.close()

    message = str(caught.value)
    assert "FLEETVIEW_UI_PORT" in message
    assert "FLEETVIEW_UI=0" in message


def test_a_free_port_is_accepted():
    import socket as socketlib

    from fleetview.daemon.ui_app import check_ui_port

    with socketlib.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    check_ui_port("127.0.0.1", port)


def test_the_projection_can_be_switched_off_for_measurement():
    """`FLEETVIEW_PROJECTION=0` is how the load gate isolates the ingest path."""
    assert Settings.from_env({"FLEETVIEW_PROJECTION": "0"}).projection_enabled is False
    assert Settings.from_env({}).projection_enabled is True
    assert Settings.from_env({"FLEETVIEW_UI": "off"}).ui_enabled is False
    assert Settings.from_env({"FLEETVIEW_UI_PORT": "9999"}).ui_port == 9999
