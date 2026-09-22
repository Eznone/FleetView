"""The UI listener's read API.

§4.1 tier 3 is the rule these routes exist to honour: the in-memory projection
is authoritative for the UI and the database is durability, never the render
path. `/v1/agents` therefore answers from memory, and `/v1/events` is for the
waterfall and the inspector -- history, not state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial

from starlette.testclient import TestClient

from fleetview.daemon.ui_app import find_ui_dist
from fleetview.schema.events import AgentState, EventType, FleetViewEvent
from fleetview.terminal.paths import agent_log_dir, open_segment
from uiharness import BASE_URL, RUN, build, emit


def event(event_type, *, agent="a1", payload=None) -> FleetViewEvent:
    return FleetViewEvent(
        run_id=RUN, agent_id=agent, channel="hook",
        event_type=event_type, payload=payload or {},
        timestamp=datetime.now(timezone.utc),
    )


def push(client, hook, *events) -> None:
    """Commit events on the portal's loop, where the store actually lives."""
    for item in events:
        client.portal.call(partial(emit, hook.state.store, item))


# --- health -------------------------------------------------------------------


def test_health_reports_the_run_and_the_cap(tmp_path):
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        body = client.get("/v1/health").json()
    assert body["status"] == "ok"
    assert body["runId"] == RUN
    # §8.5 requirement 1, and the number the UI's fleet banner states.
    assert body["maxConcurrentAgents"] == 3
    assert body["uiClients"] == 0


# --- the projection -----------------------------------------------------------


def test_agents_answers_from_memory(tmp_path):
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        push(client, hook,
             event(EventType.AGENT_READY),
             event(EventType.TASK_STARTED, payload={"prompt": "refactor auth"}))
        body = client.get("/v1/agents").json()

    assert [a["agentId"] for a in body["agents"]] == ["a1"]
    assert body["agents"][0]["state"] == AgentState.RUNNING
    assert body["maxConcurrentAgents"] == 3


def test_the_snapshot_is_camelcase_on_the_wire(tmp_path):
    """The UI consumes §5.1's TypeScript interface directly."""
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        push(client, hook, event(EventType.AGENT_READY))
        agent = client.get("/v1/agents").json()["agents"][0]

    assert "agentId" in agent and "agent_id" not in agent
    assert "blockingDependencyIds" in agent
    assert "terminalAttached" in agent


def test_blocked_edges_carry_their_dependencies(tmp_path):
    """§7's Phase 2 gate, at the API boundary."""
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        push(client, hook, event(
            EventType.MESSAGE_SENT, agent="brain",
            payload={"to": "codex-1", "orchestration": "handoff"},
        ))
        body = client.get("/v1/agents").json()

    brain = next(a for a in body["agents"] if a["agentId"] == "brain")
    assert brain["state"] == AgentState.WAITING_ON_SUBAGENT
    assert brain["blockingDependencyIds"] == ["codex-1"]
    assert body["edges"][0]["target"] == "codex-1"
    assert body["edges"][0]["orchestration"] == "handoff"


# --- the event log ------------------------------------------------------------


def test_events_are_queryable_for_the_waterfall(tmp_path):
    ui, hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        push(client, hook,
             event(EventType.AGENT_READY),
             event(EventType.TOOL_REQUESTED, payload={"tool_name": "Bash"}),
             event(EventType.TOOL_REQUESTED, agent="a2", payload={"tool_name": "Edit"}))

        every = client.get("/v1/events").json()
        assert len(every) == 3

        one = client.get("/v1/events", params={"agent": "a2"}).json()
        assert [e["agentId"] for e in one] == ["a2"]

        typed = client.get(
            "/v1/events", params={"type": EventType.TOOL_REQUESTED.value}
        ).json()
        assert len(typed) == 2
        assert typed[0]["payload"]["tool_name"] == "Bash", "payload keys stay snake_case"


def test_an_absurd_limit_is_refused_rather_than_served(tmp_path):
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        assert client.get("/v1/events", params={"limit": 10_000}).status_code == 422


# --- terminal history ---------------------------------------------------------


def test_history_replays_raw_ansi_unstripped(tmp_path):
    """xterm.js wants the escape sequences; the CLI's stripper is for grep."""
    ui, _hook, settings = build(tmp_path)
    directory = agent_log_dir(settings, "a1")
    directory.mkdir(parents=True, exist_ok=True)
    segment = open_segment(directory, 1, mode=0o600)
    segment.write_bytes(b"\x1b[32mhello\x1b[0m\r\n")

    with TestClient(ui, base_url=BASE_URL) as client:
        response = client.get("/v1/terminals/a1/history")

    assert response.status_code == 200
    assert response.content == b"\x1b[32mhello\x1b[0m\r\n"
    assert response.headers["content-type"] == "application/octet-stream"


def test_history_for_an_agent_with_no_capture_is_empty_not_an_error(tmp_path):
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        response = client.get("/v1/terminals/nobody/history")
    assert response.status_code == 200
    assert response.content == b""


def test_history_honours_the_byte_budget(tmp_path):
    ui, _hook, settings = build(tmp_path)
    directory = agent_log_dir(settings, "a1")
    directory.mkdir(parents=True, exist_ok=True)
    open_segment(directory, 1, mode=0o600).write_bytes(b"0123456789")

    with TestClient(ui, base_url=BASE_URL) as client:
        assert client.get("/v1/terminals/a1/history", params={"bytes": 4}).content == b"6789"


def test_a_bad_since_is_a_400(tmp_path):
    ui, _hook, settings = build(tmp_path)
    directory = agent_log_dir(settings, "a1")
    directory.mkdir(parents=True, exist_ok=True)
    open_segment(directory, 1, mode=0o600).write_bytes(b"x")

    with TestClient(ui, base_url=BASE_URL) as client:
        assert client.get(
            "/v1/terminals/a1/history", params={"since": "not-a-time"}
        ).status_code == 400


# --- the SPA ------------------------------------------------------------------


def test_a_missing_build_explains_itself(tmp_path, monkeypatch):
    """A silent 404 is the failure shape this project keeps finding."""
    monkeypatch.setenv("FLEETVIEW_UI_DIST", str(tmp_path / "nowhere"))
    assert find_ui_dist() is None

    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "npm run build" in response.text


def test_a_built_spa_is_served(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>FleetView</title>")
    monkeypatch.setenv("FLEETVIEW_UI_DIST", str(dist))
    assert find_ui_dist() == dist

    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        assert "FleetView" in client.get("/").text
        # The API must not be shadowed by the static mount.
        assert client.get("/v1/health").status_code == 200
