# FleetView

Run a team of coding agents and watch them work in real time — what each agent is doing, why it is
blocked, and what the agents are saying to each other. A **brain** agent delegates to **worker**
agents; FleetView is the lens you watch them through.

Local-first: a Python daemon plus a browser UI, both bound to `127.0.0.1`. Nothing leaves the
machine.

## How authentication works — and what FleetView never does

**Every agent authenticates through your own CLI subscription. There are no API keys.**

Each agent is an independent, **unmodified** vendor CLI process — `claude`, `codex` — reading its
own vendor credential store (`~/.claude`, `~/.codex`). FleetView passes `HOME` and `PATH` and gets
out of the way.

- FleetView **never reads, stores, logs, forwards or intermediates a credential.** There is no code
  path that does, and this is enforced by test, not by convention — see
  [`tests/test_compliance.py`](tests/test_compliance.py).
- FleetView runs the vendor binaries **as published**. No wrapper, no shim, no patching, no
  interception of any authentication flow. Also enforced by test.
- Each user signs in with **their own subscription**, on their own machine, through the vendor's
  own sign-in flow. FleetView never offers, proxies or brokers a login.
- `HOME` is the only route to a credential store, and that is deliberate: it is what keeps this
  architecture on the permitted side of every vendor credential rule.

FleetView is not affiliated with, endorsed by, or sponsored by Anthropic or OpenAI.

### Running agents at a sensible scale

Agents share one subscription. `max_concurrent_active_agents` defaults to **3** and is raisable
only with explicit acknowledgement, because advertised plan limits assume *ordinary, individual
usage*. On hitting a rate limit an agent **parks** and waits — FleetView never retries through a
quota wall.

## Status

**Phase 2 — the live canvas — is complete.** There is a UI: run the daemon and open
`http://127.0.0.1:8420`.

It draws every agent as a node, badged with *why* it is waiting rather than just that it is —
running, waiting on a tool, waiting on a human, waiting on its workers, rate limited, failed —
and draws the edges between them, highlighting the ones something is blocked behind. Selecting
an agent gives you its raw pane, ANSI intact, and its raw event payloads. State reaches the
browser in under 100 ms.

The browser listener is **read-only and loopback-only**. Nothing that mutates anything is
reachable over TCP: ingest and tap control stay on the Unix socket, where file permissions are
the access control. The daemon refuses to bind anywhere but loopback and validates the `Host`
and `Origin` headers, because a page you visit can otherwise reach a port on `127.0.0.1`.

**Phase 0 is complete**: a CLI spawned from a daemon-like process authenticates on the operator's
subscription, saves a transcript and resumes.

**Phase 1 — the event spine — is complete.** There are two telemetry planes, and they answer
different questions:

- **The event plane.** A spawned agent reports its whole lifecycle — session start, prompts, every
  tool call, idle, blocked — into a local SQLite store, readable with `fleetview events tail`.
  Sustains 1000 events/s with a flat ingest backlog.
- **The terminal plane.** `tmux pipe-pane` taps each agent's pane into rotating flat files, so you
  can replay exactly what an agent's screen said, ANSI and all, with `fleetview terminal tail`.
  The raw bytes **never** enter the database — it stores only where to find them, which a test
  asserts at the byte level. Logs are capped per agent (48 h / 200 MB) by the writer itself, so a
  runaway agent truncates its own log rather than filling your disk.

Phase 2 adds a third thing on top of both: an in-memory **projection** that folds the event log
into live fleet state, which is what the canvas actually renders. The database is durability; it
is never on the render path.

Still to come: delegation — the operator's own Claude session assigning work to a Codex worker.

## Development

Requires Python 3.12+, `tmux`, and at least one vendor CLI installed and signed in.

```sh
uv venv
uv pip install -e ".[dev]"
.venv/bin/python -m pytest

cd ui && npm install && npm run build && cd ..   # the browser UI
```

The daemon serves the built UI if it is there and prints the command to build it if it is not.
For frontend work, `cd ui && npm run dev` runs Vite with a proxy to the daemon, so the browser
still sees a single origin — which matters, because a cross-origin dev server is refused by the
same check that refuses an attacker.

Drive it by hand:

```sh
.venv/bin/fleetview env claude          # inspect the env an agent would receive
.venv/bin/fleetview init                # create ~/.fleetview
.venv/bin/fleetview daemon              # run the daemon + UI (foreground)
                                        # → http://127.0.0.1:8420

# ...in another shell: spawn an agent that reports to it
.venv/bin/fleetview spike claude --cwd /path/to/project --hooks
tmux attach -t <session>                # drive the agent

.venv/bin/fleetview events tail --agent <session> -f    # watch its events
.venv/bin/fleetview terminal tail <session> -f         # watch its raw output
.venv/bin/fleetview terminal ls                        # what has been captured
```

If a tap is empty and it is not obvious which half is broken, start here — it pushes a sentinel
through the whole pipeline with no agent, no tmux and no daemon:

```sh
.venv/bin/fleetview terminal selftest
```

The full suite is `pytest`; the sustained-throughput gate is separate, because it drives a real
daemon for about twenty seconds:

```sh
.venv/bin/python -m pytest -m load
```

To see the canvas without running real agents, seed a scripted fleet — a brain blocked on two
workers, one of them stalled on a permission prompt:

```sh
.venv/bin/fleetview demo seed          # then start the daemon
```

Every seeded event is marked `synthetic` and the UI shows a "demo data" ribbon, so it cannot be
mistaken for a real fleet.

Four notes if you are working on this:

- The event store lives under `~/.fleetview/` and must be on **ext4**. On WSL2 it must never sit
  under `/mnt/c` — a 9p mount where SQLite's locking is unreliable. The daemon refuses to start if
  it does.
- Hook payloads carry tool arguments, which can include file contents — and captured terminal
  bytes are whatever the agent's screen showed, which can include a sign-in URL or a token the
  agent echoed. Redaction lands in Phase 6. Until then the data is local-only and unredacted, and
  everything under `~/.fleetview/` is created `0700` (files `0600`). `fleetview init` prints the
  modes so you can see it rather than assume it.
- Keep `FLEETVIEW_HOME` short. A Unix socket path is limited to 107 bytes by the kernel; past that
  the daemon refuses to start and says so.
- `FLEETVIEW_UI=0` runs headless, `FLEETVIEW_UI_PORT` moves the listener, and
  `FLEETVIEW_PROJECTION=0` switches the canvas's read side off. The last two exist so the
  throughput gate can measure one subsystem at a time: `pytest -m load` sits almost exactly on
  the daemon's ingest ceiling, so it is run on its own, on a settled machine.

Design documentation is deliberately kept out of this repository. See `CLAUDE.md`.
