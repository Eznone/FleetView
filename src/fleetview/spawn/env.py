"""Environment construction for agent CLI processes.

This is the load-bearing module of the whole project. Two properties depend on
getting it right, and both fail *silently* when it is wrong:

1. **Subscription auth.** Agents authenticate by reading their own vendor
   credential store (``~/.claude``, ``~/.codex``). We never inject a key; we
   pass ``HOME`` and get out of the way. See PROJECT_PLAN.md §2.1.
2. **Transcript saving.** A spawned agent that inherits the *parent* session's
   identity markers believes it is a child session and quietly disables
   transcript saving — which breaks ``--resume`` for every agent of that run
   with nothing ever erroring. This bit munder-difflin live; their note records
   that no worker transcript ever reached disk for an entire run.

The layering, bottom to top:

    1. an ALLOWLIST slice of the parent environment (nothing else passes)
    2. this harness's own defaults (terminal identity, locale)
    3. per-agent values, which win over both
"""

from __future__ import annotations

import re

#: Only these keys cross from the parent environment. An allowlist rather than
#: a denylist because the set of variables a CLI might pick up on grows faster
#: than any denylist keeps up. Mirrors cli-agent-orchestrator's essential set.
ESSENTIAL_KEYS = frozenset({
    "HOME",             # the route to the vendor credential store — load-bearing
    "PATH",
    "SHELL",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "SSH_AUTH_SOCK",    # so an agent can use the operator's git ssh auth
    "DISPLAY",
    "XDG_RUNTIME_DIR",
    "DO_NOT_TRACK",
})

#: Vendor-prefixed variables the OPERATOR sets deliberately: where the CLI keeps
#: its config, which backend serves it. These describe the operator's own setup,
#: not a parent session, so they are allowed back through the prefix strip.
CONFIG_ALLOWLIST = frozenset({
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
})

#: Never forwarded, under any circumstance, even if a future edit adds one of
#: these to an allowlist above. PROJECT_PLAN.md §8.5 requirement 3: no code path
#: may read, store, log or forward a credential. ``HOME`` is the only route to
#: the credential store, and that is the entire point — it keeps us on the
#: permitted side of every vendor's credential rules.
#:
#: Note CLAUDE_CODE_OAUTH_TOKEN specifically: munder-difflin DOES forward it.
#: We deliberately do not.
CREDENTIAL_DENY = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "CODEX_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
})

#: Session-identity markers are stripped by PREFIX, not by name. munder-difflin
#: learned this the hard way: a hardcoded five-name list was already seven short
#: of what a live session actually exports.
SESSION_MARKER_RE = re.compile(r"^(CLAUDE(CODE|_)|CODEX_)")

#: Per-variable cap. Keeps the full ``tmux new-session -e`` argv under the
#: kernel's limit on a busy host.
MAX_VALUE_BYTES = 2048


def is_blocked_key(key: str) -> bool:
    """True if ``key`` must not reach a spawned agent.

    Order matters: the credential denylist is checked first and is absolute, so
    it cannot be overridden by an allowlist entry added later by mistake.
    """
    if key in CREDENTIAL_DENY:
        return True
    if key in CONFIG_ALLOWLIST:
        return False
    return bool(SESSION_MARKER_RE.match(key))


def build_agent_env(
    parent_env: dict[str, str],
    *,
    agent_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment for a spawned agent CLI.

    Pure: takes ``parent_env`` rather than reading ``os.environ``, so the
    layering is testable without mutating the process.
    """
    env: dict[str, str] = {}

    # Layer 1 — the allowlisted slice of the parent environment.
    for key, value in parent_env.items():
        if value is None:
            continue
        if is_blocked_key(key):
            continue
        if key not in ESSENTIAL_KEYS and key not in CONFIG_ALLOWLIST:
            continue
        if len(value.encode("utf-8")) >= MAX_VALUE_BYTES:
            continue
        env[key] = value

    # Layer 2 — terminal identity. The CLI must believe it is on a real
    # interactive terminal, or it renders degraded output (or none).
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["FORCE_COLOR"] = "1"

    # Locale: without this a daemon-launched child can land in the C/POSIX
    # locale, where any locale-sensitive tool the agent runs decodes UTF-8 as
    # something else and paints mojibake into the pane. LC_CTYPE only — using
    # LC_ALL would also override collation and dates for an operator who never
    # set one. A locale the operator really did export still wins.
    env["LANG"] = parent_env.get("LANG") or "en_US.UTF-8"
    env["LC_CTYPE"] = (
        parent_env.get("LC_ALL")
        or parent_env.get("LC_CTYPE")
        or parent_env.get("LANG")
        or "en_US.UTF-8"
    )

    # Layer 3 — per-agent values win over everything above, including the strip,
    # so a deliberate override is never silently discarded.
    if agent_env:
        for key, value in agent_env.items():
            if key in CREDENTIAL_DENY:
                raise ValueError(
                    f"refusing to forward credential-shaped variable {key!r} to an agent; "
                    "agents authenticate via their own credential store (see PROJECT_PLAN.md §8.5)"
                )
            env[key] = value

    return env
