"""Hook ingest: translation, the ingest endpoint, and the no-bypass reply."""

import pytest
from fastapi.testclient import TestClient

from fleetview.bus import EventBus
from fleetview.config import Settings
from fleetview.daemon.app import create_app
from fleetview.daemon.server import clear_stale_socket
from fleetview.daemon.translate import HOOK_EVENT_TYPES, translate
from fleetview.schema.events import EventType
from fleetview.store import EventStore, apply_schema, connect


def envelope(hook_name, agent_id="worker-1", **hook):
    return {
        "agentId": agent_id,
        "runId": "run-1",
        "provider": "claude",
        "hook": {"hook_event_name": hook_name, **hook},
    }


# --- translation --------------------------------------------------------------

@pytest.mark.parametrize("hook_name", sorted(HOOK_EVENT_TYPES))
def test_every_claude_hook_translates(hook_name):
    """All eight hooks in §3.4 must map. A hook wired into the agent but
    unmapped here is a silent hole in the timeline."""
    event = translate(envelope(hook_name), default_run_id="fallback")
    assert event is not None
    assert event.channel == "hook"
    assert event.agent_id == "worker-1"


def test_unknown_hook_is_dropped_not_raised():
    """A CLI update that adds a hook type must not break ingest for everything
    else."""
    assert translate(envelope("SomeFutureHook"), default_run_id="r") is None


def test_pretooluse_promotes_the_tool_fields():
    event = translate(
        envelope("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}),
        default_run_id="r",
    )
    assert event.event_type is EventType.TOOL_REQUESTED
    assert event.payload["tool_name"] == "Bash"
    assert event.payload["tool_input"] == {"command": "ls"}


def test_stop_becomes_an_idle_status_not_a_task_completion():
    """A Stop means the turn ended, which is `idle` in the §5.3 taxonomy. It
    says nothing about whether the task is done — conflating them would make
    the canvas claim work finished that is still in flight."""
    event = translate(envelope("Stop"), default_run_id="r")
    assert event.event_type is EventType.TERMINAL_STATUS_CHANGED
    assert event.payload["status"] == "idle"


def test_provider_specific_data_is_preserved_not_discarded():
    """§5.1: normalize the core fields, keep the raw blob under a namespaced
    key. The inspector exposes it as a raw-payload drawer."""
    event = translate(envelope("PreToolUse", tool_name="Edit", odd_vendor_key=1), default_run_id="r")
    assert event.provider_metadata["raw"]["odd_vendor_key"] == 1
    assert event.provider_metadata["hookEventName"] == "PreToolUse"


def test_run_id_falls_back_when_the_shim_supplies_none():
    payload = envelope("Stop")
    payload["runId"] = None
    assert translate(payload, default_run_id="daemon-run").run_id == "daemon-run"


# --- ingest -------------------------------------------------------------------

@pytest.fixture
async def client(tmp_path):
    conn = await connect(tmp_path / "d.db")
    await apply_schema(conn)
    bus = EventBus()
    store = EventStore(conn, bus=bus, settings=Settings(home=tmp_path, flush_interval_ms=10))
    await store.start()
    app = create_app(store, bus=bus, settings=Settings(home=tmp_path), run_id="run-1")
    with TestClient(app) as test_client:
        test_client.store = store
        yield test_client
    await store.stop()
    await conn.close()


async def test_hook_post_lands_in_the_store(client):
    response = client.post("/v1/events", json=envelope("PreToolUse", tool_name="Bash"))
    assert response.status_code == 200

    await client.store.flush()
    events = await client.store.fetch(agent_id="worker-1")
    assert len(events) == 1
    assert events[0].event_type is EventType.TOOL_REQUESTED


async def test_ingest_never_answers_allow(client):
    """The single most important assertion in this file. An "allow" reply would
    suppress the CLI's own permission prompt and run the fleet with the vendor
    guardrail off — the full-bypass posture §3.5.1 exists to reject. Phase 1
    has no policy engine, so the only honest answer is "no opinion"."""
    response = client.post("/v1/events", json=envelope("PreToolUse", tool_name="Bash"))
    assert response.json() == {"permissionDecision": None}


async def test_unknown_hook_is_accepted_and_ignored(client):
    response = client.post("/v1/events", json=envelope("SomeFutureHook"))
    assert response.status_code == 200
    await client.store.flush()
    assert await client.store.count() == 0


async def test_a_full_agent_session_is_captured_in_order(client):
    """The Phase 1 gate in miniature: the hook sequence a real task produces."""
    for hook in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        client.post("/v1/events", json=envelope(hook, tool_name="Edit"))
    await client.store.flush()

    events = await client.store.fetch(run_id="run-1")
    assert [e.event_type for e in events] == [
        EventType.AGENT_READY,
        EventType.TASK_STARTED,
        EventType.TOOL_REQUESTED,
        EventType.TOOL_RESULT,
        EventType.TERMINAL_STATUS_CHANGED,
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5]


def test_health_reports_the_concurrency_cap(client):
    body = client.get("/v1/health").json()
    assert body["status"] == "ok"
    assert body["maxConcurrentAgents"] == 3, "§8.5 requirement 1"


# --- operational ---------------------------------------------------------------

def test_stale_socket_file_is_removed(tmp_path):
    """An unclean exit leaves the socket file behind; binding then fails with
    "address already in use", which reads as "a daemon is already running" and
    sends the operator hunting for a process that does not exist."""
    stale = tmp_path / "daemon.sock"
    stale.touch()
    assert clear_stale_socket(stale) is True
    assert not stale.exists()


def test_absent_socket_is_not_an_error(tmp_path):
    assert clear_stale_socket(tmp_path / "missing.sock") is False


def test_subagent_stop_is_attributed_to_the_subagent():
    """Observed live on 2026-09-21: SubagentStop fires for Claude Code's own
    internal subagents, on a plain turn with no delegation. Its `agent_id` is
    the subagent's, not the worker's — two different things called agentId in
    one payload is how a consumer attributes a subagent's work to its parent."""
    payload = envelope("SubagentStop")
    # Injected into the hook blob, which is where the CLI puts it -- not into
    # our own envelope, where it would simply be our agent id.
    payload["hook"].update({"agent_id": "a830097f1f5229fd3", "agent_type": ""})
    event = translate(payload, default_run_id="r")
    assert event.agent_id == "worker-1", "our own agent id is untouched"
    assert event.payload["subagent_id"] == "a830097f1f5229fd3"
    assert event.payload["is_delegated"] is False, "an unnamed type is a CLI-internal subagent"


def test_a_named_subagent_type_is_marked_delegated():
    payload = envelope("SubagentStop")
    payload["hook"].update({"agent_id": "x", "agent_type": "code-reviewer"})
    event = translate(payload, default_run_id="r")
    assert event.payload["is_delegated"] is True
    assert event.payload["subagent_type"] == "code-reviewer"


def test_stop_carries_the_final_message():
    """Useful for the canvas later, and already present in the raw blob — the
    promotion just makes it addressable without a drawer dive."""
    event = translate(envelope("Stop", last_assistant_message="done"), default_run_id="r")
    assert event.payload["last_assistant_message"] == "done"


# --- Notification discrimination (§5.3) ---------------------------------------

@pytest.mark.parametrize("message", [
    "Claude needs your permission to use Bash",
    "Approve this action?",
    "Please confirm the edit",
    "Is this a project you trust this folder",
])
def test_permission_notifications_request_intervention(message):
    event = translate(envelope("Notification", message=message), default_run_id="r")
    assert event.event_type is EventType.HUMAN_INTERVENTION_REQUESTED
    assert event.payload["blocked_state"] == "waiting_on_human"


@pytest.mark.parametrize("message", [
    "Claude is waiting for your input",
    "Update installed. Restart to update",
])
def test_ordinary_notifications_are_only_a_status_change(message):
    """Observed live: a plain idle turn emits "Claude is waiting for your
    input". Mapping every Notification to an intervention request would raise
    the red-amber "jumps the queue" badge for every idle agent in the fleet,
    and a state that means "act now" has to stay rare to mean anything."""
    event = translate(envelope("Notification", message=message), default_run_id="r")
    assert event.event_type is EventType.TERMINAL_STATUS_CHANGED
    assert event.payload["status"] == "idle"
    assert "blocked_state" not in event.payload


def test_notification_with_no_message_is_not_an_intervention():
    event = translate(envelope("Notification"), default_run_id="r")
    assert event.event_type is EventType.TERMINAL_STATUS_CHANGED
