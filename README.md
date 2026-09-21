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

Early, and usable only from the command line. There is no UI yet.

**Phase 0 is complete**: a CLI spawned from a daemon-like process authenticates on the operator's
subscription, saves a transcript and resumes.

**Phase 1 — the event spine — is partly done.** A spawned agent reports its whole lifecycle into a
local SQLite store, which you can read back with `fleetview events tail`. Still to come: the
terminal plane, and the live canvas that makes any of this worth looking at.

## Development

Requires Python 3.12+, `tmux`, and at least one vendor CLI installed and signed in.

```sh
uv venv
uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Drive it by hand:

```sh
.venv/bin/fleetview env claude          # inspect the env an agent would receive
.venv/bin/fleetview init                # create ~/.fleetview
.venv/bin/fleetview daemon              # run the daemon (foreground)

# ...in another shell: spawn an agent that reports to it
.venv/bin/fleetview spike claude --cwd /path/to/project --hooks
tmux attach -t <session>                # drive the agent

.venv/bin/fleetview events tail --agent <session> -f    # watch its events
```

Two notes if you are working on this:

- The event store lives under `~/.fleetview/` and must be on **ext4**. On WSL2 it must never sit
  under `/mnt/c` — a 9p mount where SQLite's locking is unreliable. The daemon refuses to start if
  it does.
- Hook payloads carry tool arguments, which can include file contents. Redaction lands in Phase 6;
  until then the store is local-only and unredacted.

Design documentation is deliberately kept out of this repository. See `CLAUDE.md`.
