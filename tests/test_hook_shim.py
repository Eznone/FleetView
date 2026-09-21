"""The hook shim's two governing rules: fail open, and never emit "allow"."""

import json
import socket
import subprocess
import sys
import threading

import pytest

from fleetview.hook import shim
from fleetview.hook.install import HOOK_EVENTS, build_settings, shim_command


def pre_tool_use(tool="Bash"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": "ls"}}


# --- rule 2: never emit an allow ---------------------------------------------

def test_no_opinion_prints_nothing():
    """Silence means "carry on and ask the human as usual". This is the normal
    case for all of Phase 1 — there is no policy engine yet."""
    assert shim.decision_output(pre_tool_use(), {"permissionDecision": None}) is None


def test_an_allow_reply_is_still_not_emitted():
    """Even if the daemon says allow, the shim must not pass it on: an allow
    suppresses the CLI's own permission prompt, which is the vendor guardrail
    §3.5.1 refuses to switch off. Only deny is actionable."""
    assert shim.decision_output(pre_tool_use(), {"permissionDecision": "allow"}) is None


def test_deny_is_emitted_in_the_cli_s_own_shape():
    output = shim.decision_output(pre_tool_use(), {
        "permissionDecision": "deny",
        "reason": "writes outside the worktree",
    })
    parsed = json.loads(output)["hookSpecificOutput"]
    assert parsed["hookEventName"] == "PreToolUse"
    assert parsed["permissionDecision"] == "deny"
    assert parsed["permissionDecisionReason"] == "writes outside the worktree"


def test_non_pretooluse_hooks_never_produce_a_decision():
    """Only PreToolUse is a gate. A decision on Stop would be meaningless and
    the CLI would be right to reject it."""
    for name in ("PostToolUse", "Stop", "Notification", "SessionStart"):
        payload = {"hook_event_name": name}
        assert shim.decision_output(payload, {"permissionDecision": "deny"}) is None


# --- rule 1: fail open --------------------------------------------------------

def test_missing_daemon_exits_zero_and_says_nothing(tmp_path, monkeypatch, capsys):
    """The failure that matters: daemon down, socket absent. The agent must
    proceed into the CLI's own permission flow, not freeze mid-tool-call."""
    monkeypatch.setenv("FLEETVIEW_HOME", str(tmp_path / "nonexistent"))
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(pre_tool_use())))

    assert shim.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "", "noise on stderr would clutter the pane on every hook"


def test_unparseable_stdin_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin("not json at all"))
    assert shim.main() == 0
    assert capsys.readouterr().out == ""


def test_empty_stdin_exits_zero(monkeypatch):
    monkeypatch.setattr("sys.stdin", _Stdin(""))
    assert shim.main() == 0


def test_shim_imports_nothing_heavy():
    """The shim runs on every hook of every agent, inside the agent's own
    latency budget. Importing pydantic or fastapi here would tax every tool
    call an agent makes, for no benefit — the daemon validates on arrival."""
    source = (
        subprocess.run(
            [sys.executable, "-c",
             "import fleetview.hook.shim, sys; print(','.join(sorted(sys.modules)))"],
            capture_output=True, text=True, check=True,
        ).stdout
    )
    loaded = set(source.strip().split(","))
    assert not {"pydantic", "fastapi", "aiosqlite", "typer", "httpx"} & loaded


# --- the envelope -------------------------------------------------------------

def test_envelope_carries_the_agent_identity():
    envelope = shim.build_envelope(pre_tool_use(), {
        "FLEETVIEW_AGENT_ID": "worker-3",
        "FLEETVIEW_RUN_ID": "run-9",
        "FLEETVIEW_PROVIDER": "claude",
    })
    assert envelope["agentId"] == "worker-3"
    assert envelope["runId"] == "run-9"
    assert envelope["hook"]["tool_name"] == "Bash"


def test_envelope_tolerates_a_missing_agent_id():
    """An un-tagged hook is still worth ingesting; it just cannot be attributed."""
    assert shim.build_envelope(pre_tool_use(), {})["agentId"] is None


# --- round trip over a real socket -------------------------------------------

def test_post_round_trips_over_a_unix_socket(tmp_path):
    socket_path = tmp_path / "d.sock"
    reply = {"permissionDecision": "deny", "reason": "nope"}
    server = _FakeDaemon(socket_path, reply)
    server.start()
    try:
        got = shim.post(json.dumps({"hook": {}}).encode(), str(socket_path))
    finally:
        server.stop()
    assert got == reply


# --- generated hooks config ---------------------------------------------------

def test_every_claude_hook_is_wired():
    hooks = build_settings()["hooks"]
    assert set(hooks) == set(HOOK_EVENTS)


def test_tool_hooks_carry_a_matcher_and_others_do_not():
    hooks = build_settings()["hooks"]
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    assert "matcher" not in hooks["Stop"][0]


def test_shim_command_is_an_absolute_interpreter_path():
    """The agent's PATH is the operator's login-shell PATH and will not contain
    this virtualenv. A bare `python` resolves to something else or to nothing —
    the ENOENT class §2.1 is about."""
    command = shim_command()
    assert command.startswith("/"), command
    assert command.endswith("-m fleetview.hook")


def test_written_settings_are_valid_json(tmp_path):
    from fleetview.config import Settings

    path = write_settings(tmp_path)
    assert json.loads(path.read_text())["hooks"]["PreToolUse"]
    assert path.name.endswith(".settings.json")
    assert Settings(home=tmp_path).home == tmp_path


def write_settings(tmp_path):
    from fleetview.config import Settings
    from fleetview.hook.install import write_agent_settings

    return write_agent_settings("agent-1", settings=Settings(home=tmp_path))


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


class _FakeDaemon:
    """A minimal HTTP-over-AF_UNIX responder, to exercise the real socket path."""

    def __init__(self, path, reply):
        self.path = str(path)
        self.reply = reply
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._thread = None

    def start(self):
        self._sock.bind(self.path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            body = json.dumps(self.reply).encode()
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )

    def stop(self):
        self._sock.close()
        if self._thread:
            self._thread.join(timeout=2)
