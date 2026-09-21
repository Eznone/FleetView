"""Daemon start/stop as a real process.

This is the one place the suite pays for a subprocess, and it is worth it: the
bug it guards against is invisible from inside the process. uvicorn's
``capture_signals`` re-raises SIGTERM to the default handler once ``serve()``
returns, so anything written after ``await server.serve()`` never runs. An
in-process test cannot see that — only signalling a real daemon can.

Symptoms when it regresses: the Unix socket survives every shutdown (so the
"removed stale socket" warning fires on every start and stops meaning
anything), queued events are lost, and the SQLite WAL is never checkpointed.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

STARTUP_TIMEOUT = 20.0
SHUTDOWN_TIMEOUT = 10.0


@pytest.fixture
def daemon(tmp_path):
    env = {**os.environ, "FLEETVIEW_HOME": str(tmp_path)}
    process = subprocess.Popen(
        [sys.executable, "-m", "fleetview.cli", "daemon"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    socket_path = tmp_path / "daemon.sock"
    _wait_until(lambda: socket_path.exists(), STARTUP_TIMEOUT, process)
    yield process, tmp_path
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def test_sigterm_shuts_down_cleanly(daemon):
    process, home = daemon
    assert (home / "fleetview.db-wal").exists(), "WAL should be active while running"

    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=SHUTDOWN_TIMEOUT) is not None

    assert not (home / "daemon.sock").exists(), (
        "the socket survived shutdown — cleanup is running after serve() returns, "
        "where uvicorn's re-raised SIGTERM kills the process first"
    )
    assert not (home / "fleetview.db-wal").exists(), (
        "the WAL was never checkpointed, so the database connection was never closed"
    )
    assert (home / "fleetview.db").exists()


def test_events_posted_before_shutdown_are_not_lost(daemon):
    """The writer batches on a 50ms window, so a stop that does not drain the
    queue loses whatever is in flight. Post and immediately terminate."""
    process, home = daemon
    _post(home / "daemon.sock", {
        "agentId": "worker-1",
        "runId": "run-1",
        "provider": "claude",
        "hook": {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
    })
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=SHUTDOWN_TIMEOUT)

    import sqlite3

    conn = sqlite3.connect(home / "fleetview.db")
    try:
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    assert count == 1, "an event acknowledged over the socket was dropped on shutdown"


def _post(socket_path, payload):
    body = json.dumps(payload).encode()
    request = (
        b"POST /v1/events HTTP/1.1\r\nHost: fleetview\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n" + body
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(socket_path))
        sock.sendall(request)
        while sock.recv(65536):
            pass


def _wait_until(predicate, timeout, process):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            pytest.fail(f"daemon exited early: {process.stdout.read()}")
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail("daemon did not start in time")
