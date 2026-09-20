"""Resolving a bare CLI name (``claude``) to an executable path.

A daemon does not inherit the login shell's PATH, so a bare ``claude`` fails
with ENOENT even though the operator can run it fine in their terminal. Node
version managers (nvm, asdf, volta) and Homebrew all edit PATH from shell rc
files that a non-interactive process never sources.

munder-difflin's fix, ported: ask the operator's own interactive login shell
what its PATH is — but fence the answer, because an interactive shell also runs
rc files that are free to print. A plain ``.strip()`` on the output of
``echo $PATH`` yields ``"Restored session: <date>\\n/opt/homebrew/bin:..."`` on
any zsh with a session-restore plugin, and that whole string then becomes the
PATH handed to every agent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

#: A plain executable name. This token may be interpolated into a shell command
#: below, so it is constrained to characters unambiguously part of a binary
#: name; anything else is refused rather than resolved.
SAFE_COMMAND_RE = re.compile(r"^[A-Za-z0-9._+-]+$")

_FENCE = "__FLEETVIEW_SHELL_FENCE__"

#: Known install locations, tried in order when the shell capture fails.
_FALLBACK_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.claude/local",
    "~/.volta/bin",
    "~/.bun/bin",
)

# Each capture boots a full interactive login shell — hundreds of milliseconds
# of blocking work — and PATH does not change mid-session. Only successful
# captures are cached, so a failure stays retryable.
_path_cache: str | None = None


def capture_login_shell_path(*, timeout: float = 3.0) -> str | None:
    """Return the operator's interactive-login-shell PATH, or None.

    Fenced between markers so rc-file chatter before or after the value cannot
    be mistaken for part of it.
    """
    shell = os.environ.get("SHELL") or "/bin/sh"
    script = f'printf %s {_FENCE}; printf %s "$PATH"; printf %s {_FENCE}'
    try:
        proc = subprocess.run(
            [shell, "-ilc", script],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    out = proc.stdout or ""
    start, end = out.find(_FENCE), out.rfind(_FENCE)
    if start < 0 or end <= start:
        return None
    value = out[start + len(_FENCE):end].strip()

    # A PATH is one colon-joined line. Anything multi-line is rc-file noise that
    # slipped the fence — better to fall back than to hand an agent a corrupt
    # PATH it would then carry into every subprocess it spawns.
    if not value or "\n" in value:
        return None
    return value


def user_path() -> str:
    """The PATH agents should be given. Cached for the process lifetime."""
    global _path_cache
    if _path_cache is None:
        _path_cache = capture_login_shell_path() or os.environ.get("PATH", "")
    return _path_cache


def resolve_cli_binary(command: str, *, search_path: str | None = None) -> str:
    """Resolve ``command`` to an executable path.

    Returns the vendor binary as published — never a wrapper or shim we wrote.
    PROJECT_PLAN.md §8.5 requirement 4: vendor terms require the binary be run
    unmodified, so this function must not interpose anything.
    """
    # An explicit path is passed through untouched.
    if "/" in command:
        return command
    if not SAFE_COMMAND_RE.match(command):
        raise ValueError(f"not a plain executable name: {command!r}")

    path = search_path if search_path is not None else user_path()

    found = shutil.which(command, path=path)
    if found:
        return found

    for directory in _FALLBACK_DIRS:
        candidate = Path(directory).expanduser() / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    raise FileNotFoundError(
        f"could not resolve {command!r} on the login-shell PATH or in any known install "
        f"location. Is it installed for this user?"
    )
