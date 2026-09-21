"""Forward one CLI lifecycle hook to the daemon. Stdlib only — see __init__.

Invoked by the vendor CLI with the hook payload on stdin. Posts it to the
daemon over a Unix socket and, for ``PreToolUse``, waits for a decision.

Two rules govern everything here, and both are load-bearing:

**1. Fail open.** If the daemon is down, slow, or the socket is missing, this
exits 0 having printed nothing, and the agent proceeds into the CLI's own
permission flow. Risk 3's corollary in PROJECT_PLAN.md: a worker whose hook
fails to install degrades to the vendor's guardrail rather than to nothing. The
alternative — failing closed — would mean a daemon crash silently freezes every
agent in the fleet mid-tool-call.

**2. Never emit "allow".** FleetView's layer is a policy *pre-filter*, not the
human gate (§3.5.1). Returning ``permissionDecision: "allow"`` would suppress
the CLI's own prompt and switch off the vendor guardrail — precisely the
full-bypass posture §3.5.1 exists to avoid. Silence means "no opinion, carry
on and ask the human as usual"; only an explicit *deny* is ever emitted.
"""

from __future__ import annotations

import json
import os
import socket
import sys

#: Generous enough for a busy daemon, short enough that a dead one does not
#: visibly stall the agent. The CLI applies its own hook timeout on top.
TIMEOUT_SECONDS = 2.0

_ENDPOINT = "/v1/events"


def default_socket_path() -> str:
    home = os.environ.get("FLEETVIEW_HOME") or "~/.fleetview"
    return os.path.expanduser(os.path.join(home, "daemon.sock"))


def post(body: bytes, socket_path: str, timeout: float = TIMEOUT_SECONDS) -> dict | None:
    """POST ``body`` to the daemon over AF_UNIX. Returns the parsed reply, or None.

    Hand-rolled HTTP because the alternative is importing an HTTP client on
    every hook invocation. The request is trivially small and fully under our
    control, so there is no content negotiation to get wrong.
    """
    request = (
        f"POST {_ENDPOINT} HTTP/1.1\r\n"
        "Host: fleetview\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode() + body

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(request)

        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)

    raw = b"".join(chunks)
    separator = raw.find(b"\r\n\r\n")
    if separator < 0:
        return None
    payload = raw[separator + 4:]
    if not payload.strip():
        return None
    return json.loads(payload)


def build_envelope(hook_payload: dict, environ: dict[str, str]) -> dict:
    """Tag the vendor's hook payload with the identity the daemon needs.

    ``FLEETVIEW_AGENT_ID`` reaches the agent through the per-agent env layer in
    ``spawn/env.py``, which is applied after the session-marker strip precisely
    so a deliberate value like this survives it.
    """
    return {
        "agentId": environ.get("FLEETVIEW_AGENT_ID"),
        "runId": environ.get("FLEETVIEW_RUN_ID"),
        "provider": environ.get("FLEETVIEW_PROVIDER", "claude"),
        "hook": hook_payload,
    }


def decision_output(hook_payload: dict, reply: dict | None) -> str | None:
    """The JSON to print on stdout, or None to stay silent.

    Silence is the default and the safe answer. See rule 2 in the module
    docstring: an "allow" here would disable the CLI's own permission prompt.
    """
    if not reply:
        return None
    if hook_payload.get("hook_event_name") != "PreToolUse":
        return None

    decision = reply.get("permissionDecision")
    if decision != "deny":
        return None

    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reply.get("reason", "denied by FleetView policy"),
        }
    })


def main(argv: list[str] | None = None) -> int:
    try:
        raw = sys.stdin.read()
        hook_payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # Unparseable input is the CLI's problem, not the agent's.

    try:
        envelope = build_envelope(hook_payload, dict(os.environ))
        reply = post(json.dumps(envelope).encode(), default_socket_path())
        output = decision_output(hook_payload, reply)
        if output:
            print(output)
    except Exception:
        # Fail open, and say nothing. Rule 1. An error message on stdout would
        # be parsed by the CLI as hook output; on stderr it would clutter the
        # operator's pane on every hook while the daemon is down.
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
