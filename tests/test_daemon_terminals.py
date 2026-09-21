"""The /v1/terminals routes."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.daemon.app import create_app
from fleetview.spawn.tmux import TerminalTapError
from fleetview.store import EventStore, TerminalChunkStore, apply_schema, connect


class FakeHandle:
    def __init__(self, agent_id, session):
        from datetime import datetime, timezone
        self.agent_id = agent_id
        self.tmux_session = session
        self.fifo = f"/run/{agent_id}.fifo"
        self.attached_at = datetime(2026, 9, 21, tzinfo=timezone.utc)


class FakeSupervisor:
    def __init__(self, *, fail: Exception | None = None):
        self.fail = fail
        self.attached: dict[str, FakeHandle] = {}
    async def attach(self, agent_id, tmux_session):
        if self.fail:
            raise self.fail
        handle = FakeHandle(agent_id, tmux_session)
        self.attached[agent_id] = handle
        return handle
    async def detach(self, agent_id):
        return self.attached.pop(agent_id, None) is not None
    def status(self):
        return [{"agentId": a} for a in self.attached]
    @property
    def count(self):
        return len(self.attached)


async def client(tmp_path, supervisor=None):
    settings = Settings(home=tmp_path)
    conn = await connect(tmp_path / "t.db")
    await apply_schema(conn)
    bus = EventBus()
    store = EventStore(conn, bus=bus, settings=settings)
    await store.start()
    app = create_app(store, bus=bus, settings=settings)
    app.state.terminals = supervisor or FakeSupervisor()
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://fv")
    return c, app, conn, store


async def test_attach_returns_201_with_the_fifo(tmp_path):
    c, app, conn, store = await client(tmp_path)
    response = await c.post("/v1/terminals",
                            json={"agentId": "agent-1", "tmuxSession": "sess-1"})
    assert response.status_code == 201
    body = response.json()
    assert body["agentId"] == "agent-1"
    assert body["fifo"].endswith("agent-1.fifo")
    await store.stop(); await conn.close(); await c.aclose()


async def test_a_missing_session_is_404(tmp_path):
    sup = FakeSupervisor(fail=TerminalTapError("no tmux session 'ghost'"))
    c, app, conn, store = await client(tmp_path, sup)
    response = await c.post("/v1/terminals",
                            json={"agentId": "a", "tmuxSession": "ghost"})
    assert response.status_code == 404
    await store.stop(); await conn.close(); await c.aclose()


async def test_a_tap_that_will_not_verify_is_409(tmp_path):
    """Not a 500: the pane exists, it just refused to stay piped."""
    sup = FakeSupervisor(fail=TerminalTapError("pipe-pane reported success but "
                                               "#{pane_pipe} is not 1 — not capturing"))
    c, app, conn, store = await client(tmp_path, sup)
    response = await c.post("/v1/terminals",
                            json={"agentId": "a", "tmuxSession": "s"})
    assert response.status_code == 409
    assert "not capturing" in response.json()["detail"]
    await store.stop(); await conn.close(); await c.aclose()


async def test_an_unknown_field_is_refused(tmp_path):
    """extra='forbid': a typo'd field should be a 422, not a silently dropped
    key that leaves the caller thinking it asked for something it did not."""
    c, app, conn, store = await client(tmp_path)
    response = await c.post("/v1/terminals",
                            json={"agentId": "a", "tmuxSession": "s", "tmux": "oops"})
    assert response.status_code == 422
    await store.stop(); await conn.close(); await c.aclose()


async def test_snake_case_is_also_accepted(tmp_path):
    c, app, conn, store = await client(tmp_path)
    response = await c.post("/v1/terminals",
                            json={"agent_id": "a", "tmux_session": "s"})
    assert response.status_code == 201
    await store.stop(); await conn.close(); await c.aclose()


async def test_detach_reports_whether_anything_happened(tmp_path):
    c, app, conn, store = await client(tmp_path)
    assert (await c.delete("/v1/terminals/nobody")).json() == {"detached": False}
    await c.post("/v1/terminals", json={"agentId": "a", "tmuxSession": "s"})
    assert (await c.delete("/v1/terminals/a")).json() == {"detached": True}
    await store.stop(); await conn.close(); await c.aclose()


async def test_listing_is_empty_until_something_attaches(tmp_path):
    c, app, conn, store = await client(tmp_path)
    assert (await c.get("/v1/terminals")).json() == []
    await c.post("/v1/terminals", json={"agentId": "a", "tmuxSession": "s"})
    assert (await c.get("/v1/terminals")).json() == [{"agentId": "a"}]
    await store.stop(); await conn.close(); await c.aclose()


async def test_health_reports_terminals_and_the_ingest_backlog(tmp_path):
    c, app, conn, store = await client(tmp_path)
    body = (await c.get("/v1/health")).json()
    assert body["terminals"] == 0
    assert body["pending"] == 0
    await c.post("/v1/terminals", json={"agentId": "a", "tmuxSession": "s"})
    assert (await c.get("/v1/health")).json()["terminals"] == 1
    await store.stop(); await conn.close(); await c.aclose()
