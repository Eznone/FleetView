"""Phase 1's load gate: sustained 1k events/s with no ingest stall.

Marked `load` and excluded from the default run, so the ordinary suite stays
fast — but it must stay *routinely runnable* (`pytest -m load`), because a gate
that is run once and then rots proves nothing about the code as it is now.

**Why the full path.** §4.1 already measured SQLite at ~242k events/s unbatched,
so re-measuring SQLite would prove nothing. What has never been measured is
uvicorn over a Unix socket, FastAPI routing, `translate`, and the group-commit
queue — which is exactly where a stall would live.

**"No stall" is four properties, not a number.** Throughput alone can look
perfect while the daemon buffers everything in RAM and would die at 20 s, so
the queue-depth assertion is the one that actually distinguishes "kept up" from
"fell behind quietly".
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.load

TARGET_RATE = 1000          # events/s, §7's gate
DURATION_SECONDS = 10
TOTAL = TARGET_RATE * DURATION_SECONDS
AGENTS = [f"load-agent-{i}" for i in range(5)]
RUNS = ["load-run-a", "load-run-b"]

#: The shim gives up at 2 s (hook/shim.py TIMEOUT_SECONDS) and holds a worker's
#: tool call open until then. Asserting against a fraction of that real budget
#: is the point — "feels fast" is not a gate.
P99_BUDGET_MS = 50
MAX_BUDGET_MS = 500

#: Requests in flight.
#:
#: Modelled on the real producer, not on "as much load as possible". Each hook
#: is a separate short-lived shim process making **one** blocking request, and
#: §8.5 requirement 1 caps the fleet at 3 concurrent agents — so a handful in
#: flight is what actually happens.
#:
#: It also matters empirically. Measured on the Phase 1 machine at a 1000/s
#: target: 4 in flight sustains 999/s at p99 5.6 ms, 8 sustains 983/s at
#: 26 ms, 16 collapses to 823/s at 74 ms, and 32 to 656/s at 222 ms. Beyond a
#: handful of connections the *driver* becomes the bottleneck and the gate
#: stops measuring the daemon at all.
CONCURRENCY = 4


def _free_port() -> int:
    """An ephemeral port the UI listener can have to itself.

    The daemon binds a TCP port from Phase 2 onward, so a fixed one would
    collide with the operator's own running daemon and with the previous test
    in the same session -- and a bind failure surfaces as "the daemon did not
    start", which says nothing about why.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _spawn_daemon(tmp_path, **extra_env):
    env = {**os.environ, "FLEETVIEW_HOME": str(tmp_path),
           "FLEETVIEW_TERMINAL_CAPTURE": "0", **extra_env}
    process = subprocess.Popen(
        [sys.executable, "-m", "fleetview.cli", "daemon"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    socket_path = tmp_path / "daemon.sock"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if socket_path.exists():
            break
        if process.poll() is not None:
            pytest.fail(f"daemon exited early: {process.stdout.read()}")
        time.sleep(0.05)
    else:
        pytest.fail("daemon did not start")
    return process


def _stop(process) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


@pytest.fixture
def daemon(tmp_path):
    """The ingest path, isolated.

    Terminal capture and the projection are both off, and so is the UI
    listener -- the same reasoning in all three cases. F9 puts the ingest
    ceiling at ~1000 events/s with this gate sitting exactly on it, so a
    measurement that bundles every subsystem together tells you a regression
    happened without telling you where. `test_the_gate_holds_with_a_live_ui_
    client_attached` measures the loaded configuration separately.
    """
    process = _spawn_daemon(
        tmp_path, FLEETVIEW_UI="0", FLEETVIEW_PROJECTION="0",
    )
    yield process, tmp_path
    _stop(process)


@pytest.fixture
def ui_daemon(tmp_path):
    """The Phase 2 configuration: projection on, UI listening, on its own port."""
    port = _free_port()
    process = _spawn_daemon(tmp_path, FLEETVIEW_UI="1", FLEETVIEW_UI_PORT=str(port))
    yield process, tmp_path, port
    _stop(process)


def _envelope(i: int) -> dict:
    return {
        "agentId": AGENTS[i % len(AGENTS)],
        "runId": RUNS[i % len(RUNS)],
        "provider": "claude",
        "hook": {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": f"/src/module_{i}.py"},
            "session_id": f"sess-{i % 7}",
        },
    }


async def _drive(socket_path, samples, latencies, statuses):
    transport = httpx.AsyncHTTPTransport(
        uds=str(socket_path),
        limits=httpx.Limits(max_connections=CONCURRENCY,
                            max_keepalive_connections=CONCURRENCY),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://fv",
                                 timeout=10.0) as client:
        semaphore = asyncio.Semaphore(CONCURRENCY)
        started = time.monotonic()

        async def one(i: int) -> None:
            # Token-bucket pacing: each request has a scheduled departure, so a
            # slow daemon shows up as the pacer falling behind rather than as
            # the test quietly slowing down to match it.
            due = started + i / TARGET_RATE
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            async with semaphore:
                t0 = time.perf_counter()
                response = await client.post("/v1/events", json=_envelope(i))
                latencies.append((time.perf_counter() - t0) * 1000)
                statuses.append(response.status_code)

        async def sample() -> None:
            while True:
                await asyncio.sleep(0.25)
                try:
                    health = await client.get("/v1/health")
                    samples.append(health.json()["pending"])
                except Exception:
                    return

        sampler = asyncio.create_task(sample())
        await asyncio.gather(*(one(i) for i in range(TOTAL)))
        elapsed = time.monotonic() - started
        sampler.cancel()
        return elapsed


def test_sustained_1k_events_per_second_without_an_ingest_stall(daemon):
    process, home = daemon
    latencies: list[float] = []
    statuses: list[int] = []
    samples: list[int] = []

    elapsed = asyncio.run(_drive(home / "daemon.sock", samples, latencies, statuses))

    # --- 1. producer-visible latency ---------------------------------------
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99)]
    worst = latencies[-1]
    assert p99 < P99_BUDGET_MS, (
        f"p99 POST latency {p99:.1f} ms exceeds {P99_BUDGET_MS} ms; the shim "
        f"holds a worker's tool call open while this is happening"
    )
    assert worst < MAX_BUDGET_MS, f"worst POST latency {worst:.1f} ms"

    # --- 2. sustained rate --------------------------------------------------
    achieved = TOTAL / elapsed
    assert achieved >= TARGET_RATE * 0.95, (
        f"only sustained {achieved:.0f} events/s against a {TARGET_RATE}/s target"
    )

    # --- 3. bounded queue ---------------------------------------------------
    # The assertion that separates "kept up" from "buffered everything in RAM".
    assert samples, "never sampled the ingest backlog"
    quarter = max(1, len(samples) // 4)
    second_quartile = statistics.mean(samples[quarter:quarter * 2])
    final_quartile = statistics.mean(samples[-quarter:])
    assert final_quartile <= 500 * 2, (
        f"ingest backlog averaged {final_quartile:.0f} in the final quartile"
    )
    if second_quartile > 1:
        assert final_quartile <= second_quartile * 2, (
            f"backlog grew monotonically ({second_quartile:.0f} -> "
            f"{final_quartile:.0f}) — that is the stall"
        )

    # --- 4. completeness and ordering --------------------------------------
    assert set(statuses) == {200}, f"non-200 responses: {set(statuses)}"

    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=30) is not None

    conn = sqlite3.connect(home / "fleetview.db")
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        assert count == TOTAL, f"{count} events durable out of {TOTAL} posted"

        # Single-writer sequence assignment is the correctness property the
        # throughput number exists to protect: gaps or duplicates here would
        # make replay ambiguous, and replay rebuilds every projection.
        for run_id in RUNS:
            rows = [r[0] for r in conn.execute(
                "SELECT sequence FROM events WHERE run_id = ? ORDER BY sequence",
                (run_id,))]
            assert rows == list(range(1, len(rows) + 1)), (
                f"run {run_id} has gaps or duplicates in its sequence"
            )
    finally:
        conn.close()

    print(f"\n  sustained {achieved:.0f} events/s · p99 {p99:.1f} ms · "
          f"max {worst:.1f} ms · backlog {second_quartile:.0f}->{final_quartile:.0f}")


def test_the_gate_holds_with_a_live_ui_client_attached(ui_daemon):
    """The Phase 2 configuration, under the Phase 1 load.

    F9 is the reason this test exists: the ingest ceiling is ~1000 events/s and
    the gate sits exactly on it, so a WebSocket fan-out task on the same loop
    is precisely the kind of addition that regresses it. It already did once --
    publishing a fleet snapshot per event cost ~9% of throughput before the
    broadcast was coalesced (`projection_broadcast_ms`).

    The rate assertion is deliberately looser here than in the isolated gate.
    That gate owns the throughput number; this one exists to catch a *stall* --
    a growing backlog or a latency cliff -- and on this machine the run-to-run
    spread of the same configuration is wide enough that a tight rate bound
    here would fail for reasons that have nothing to do with the UI.
    """
    import websockets

    process, home, port = ui_daemon
    latencies: list[float] = []
    statuses: list[int] = []
    samples: list[int] = []
    frames = 0

    async def run() -> float:
        nonlocal frames
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/live") as socket_:
            async def drain() -> None:
                nonlocal frames
                while True:
                    await socket_.recv()
                    frames += 1

            drainer = asyncio.create_task(drain())
            try:
                return await _drive(home / "daemon.sock", samples, latencies, statuses)
            finally:
                drainer.cancel()

    elapsed = asyncio.run(run())

    assert frames > 0, "the client was attached but the daemon never fed it"
    assert set(statuses) == {200}, f"non-200 responses: {set(statuses)}"

    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99)]
    assert p99 < P99_BUDGET_MS, (
        f"p99 POST latency {p99:.1f} ms with a UI client attached; a hook shim "
        f"holds a worker's tool call open while this is happening"
    )

    achieved = TOTAL / elapsed
    assert achieved >= TARGET_RATE * 0.75, (
        f"only sustained {achieved:.0f} events/s with a UI client attached"
    )

    # The stall signal, which is what this test is really for.
    assert samples, "never sampled the ingest backlog"
    quarter = max(1, len(samples) // 4)
    second_quartile = statistics.mean(samples[quarter:quarter * 2])
    final_quartile = statistics.mean(samples[-quarter:])
    if second_quartile > 1:
        assert final_quartile <= second_quartile * 2, (
            f"backlog grew monotonically ({second_quartile:.0f} -> "
            f"{final_quartile:.0f}) with a UI client attached"
        )

    print(f"\n  with UI client: {achieved:.0f} events/s · p99 {p99:.1f} ms · "
          f"{frames} frames · backlog {second_quartile:.0f}->{final_quartile:.0f}")


def test_ingest_latency_is_unaffected_by_terminal_capture(tmp_path):
    """Both planes at once — the only test that exercises them together.

    This is what catches a libtmux call left on the event loop: reconcile()
    shells out synchronously, and a 30 ms block would land here as a p99
    regression rather than as an obvious failure anywhere else.
    """
    # Terminal capture on, everything else off: this test is about one plane,
    # and leaving the UI listener enabled would also make it fight the other
    # load tests over a TCP port.
    env = {**os.environ, "FLEETVIEW_HOME": str(tmp_path),
           "FLEETVIEW_TERMINAL_CAPTURE": "1",
           "FLEETVIEW_UI": "0", "FLEETVIEW_PROJECTION": "0"}
    process = subprocess.Popen(
        [sys.executable, "-m", "fleetview.cli", "daemon"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    socket_path = tmp_path / "daemon.sock"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not socket_path.exists():
        if process.poll() is not None:
            pytest.fail(f"daemon exited early: {process.stdout.read()}")
        time.sleep(0.05)

    try:
        latencies: list[float] = []
        statuses: list[int] = []
        samples: list[int] = []
        asyncio.run(_drive(socket_path, samples, latencies, statuses))

        latencies.sort()
        p99 = latencies[int(len(latencies) * 0.99)]
        assert p99 < P99_BUDGET_MS, (
            f"p99 {p99:.1f} ms with terminal capture enabled, against a "
            f"{P99_BUDGET_MS} ms budget. Compare the capture-disabled gate "
            f"above: if that one passes and this one does not, something in "
            f"the terminal plane is blocking the event loop — a libtmux call "
            f"outside asyncio.to_thread is the usual cause."
        )
        assert set(statuses) == {200}
        print(f"\n  with capture enabled: p99 {p99:.1f} ms")
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=30)
