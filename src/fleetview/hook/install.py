"""Generating the per-agent hooks configuration a spawned CLI is started with.

Claude Code takes ``--settings <file>``; Codex has no equivalent and will need
a per-agent ``CODEX_HOME`` instead (§6.2), which is Phase 4's problem.

This module imports from FleetView freely — unlike :mod:`fleetview.hook.shim`,
it runs once at spawn time, not on every hook.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fleetview.config import Settings

#: Every hook Claude Code exposes (§3.4). All of them are wired: the cost of an
#: extra hook is one short-lived process, and a missing one is a blind spot
#: that only shows up as a gap in the timeline much later.
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SubagentStop",
    "PostCompact",
)

#: Hooks that fire per tool call and therefore take a matcher.
TOOL_HOOKS = frozenset({"PreToolUse", "PostToolUse"})

#: The CLI's own ceiling on how long it waits for the shim. Comfortably above
#: the shim's own 2s socket timeout, so the shim's fail-open path runs and
#: returns cleanly rather than being killed partway.
HOOK_TIMEOUT_SECONDS = 5


def shim_command(python: str | None = None) -> str:
    """The command the CLI runs for each hook.

    An **absolute** interpreter path, deliberately. The agent's PATH is the
    operator's login-shell PATH (see ``spawn/resolve.py``) and will not contain
    this virtualenv, so a bare ``python`` or ``fleetview-hook`` resolves to
    something else or to nothing — the ENOENT class §2.1 is entirely about.
    """
    return f"{python or sys.executable} -m fleetview.hook"


def build_settings(python: str | None = None) -> dict:
    command = shim_command(python)
    entry = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}

    hooks: dict[str, list[dict]] = {}
    for event in HOOK_EVENTS:
        block: dict = {"hooks": [entry]}
        if event in TOOL_HOOKS:
            block["matcher"] = "*"
        hooks[event] = [block]

    return {"hooks": hooks}


def write_agent_settings(
    agent_id: str,
    *,
    settings: Settings | None = None,
    python: str | None = None,
) -> Path:
    """Write the hooks config for one agent and return its path."""
    settings = settings or Settings.from_env()
    directory = settings.home / "hooks"
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"{agent_id}.settings.json"
    path.write_text(json.dumps(build_settings(python), indent=2))
    return path
