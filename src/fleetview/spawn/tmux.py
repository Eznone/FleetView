"""Creating the detached tmux session an agent CLI runs inside.

tmux rather than a direct PTY, for three reasons (PROJECT_PLAN.md §4):
sessions survive daemon *and* UI restarts, the operator can ``tmux attach``
directly at any time, and ``pipe-pane`` gives a clean byte tap for the terminal
plane later.
"""

from __future__ import annotations

import contextlib
import shlex
from pathlib import Path

import libtmux

#: Detached sessions default to 80x24. Some CLI TUIs fail to repaint after the
#: SIGWINCH that arrives when the operator later attaches at their real terminal
#: size — the pane goes blank and input is silently dropped. Starting large
#: makes that resize a no-op or a shrink, which they handle correctly.
DEFAULT_COLS = 220
DEFAULT_ROWS = 50


class AgentSpawnError(RuntimeError):
    """Raised when a session could not be created."""


def build_window_command(argv: list[str]) -> str:
    """Escape an argv LIST into a single command string for tmux.

    tmux's window command is a string it hands to the shell, so something must
    do the escaping. Doing it here, once, from a list, is the point:
    munder-difflin's workerLaunch.ts exists because a caller-authored command
    *line* was passed through unsplit, making the exec layer look for a binary
    named like the entire string. Workers died about a second after spawn while
    their request archived as successfully done — silent for days.

    So: callers build a list, this function escapes it, and no caller-authored
    string ever reaches a shell unescaped.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    return shlex.join(argv)


def create_agent_session(
    *,
    session_name: str,
    argv: list[str],
    cwd: str,
    env: dict[str, str],
    server: libtmux.Server | None = None,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
) -> libtmux.Session:
    """Create a detached tmux session running ``argv``.

    ``env`` must come from :func:`fleetview.spawn.env.build_agent_env` — this
    function does not sanitise it, deliberately, so there is exactly one place
    where environment policy lives.
    """
    srv = server if server is not None else libtmux.Server()

    if srv.has_session(session_name):
        raise AgentSpawnError(
            f"tmux session {session_name!r} already exists — "
            f"kill it first (tmux kill-session -t {session_name})"
        )

    try:
        session = srv.new_session(
            session_name=session_name,
            start_directory=cwd,
            attach=False,
            window_command=build_window_command(argv),
            environment=env,
            x=cols,
            y=rows,
        )
    except Exception as exc:  # libtmux raises a variety of types
        raise AgentSpawnError(f"could not create tmux session {session_name!r}: {exc}") from exc

    return session


def capture_pane(session: libtmux.Session, *, lines: int = 200) -> str:
    """Read recent pane output — the crude status check the spike needs.

    Kept, not abandoned: §5.3 renders a `failed` agent "Red, with PTY tail",
    and this is where that tail comes from. The terminal plane supersedes it
    for *streaming* (see :mod:`fleetview.terminal`), but a one-shot scrape of a
    pane that died before its tap was attached has no other source."""
    pane = session.active_window.active_pane
    if pane is None:
        return ""
    return "\n".join(pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout)


# --- the terminal tap (channel B) -------------------------------------------
#
# tmux knowledge lives in this module, so `pipe-pane` belongs here rather than
# under terminal/, even though the daemon is what calls it.


class TerminalTapError(RuntimeError):
    """Raised when a pane's byte tap could not be established or verified."""


def pipe_pane_command(fifo_path: Path | str) -> str:
    """Build the shell command tmux runs beside the pane.

    tmux hands this string to ``/bin/sh -c``, so this is the one layer that
    needs quoting -- libtmux passes argv straight to the tmux binary with no
    shell in between.

    ``#`` is rejected rather than escaped: tmux expands the status-left
    sequences in this string (``#I`` becomes the window index, and so on), so a
    ``#`` in a path silently redirects the pipe into a different file than the
    one we recorded. Agent ids are already constrained by
    :func:`fleetview.terminal.paths.agent_slug`; this catches the case where it
    arrives via ``$HOME`` instead.
    """
    text = str(fifo_path)
    if "#" in text:
        raise TerminalTapError(
            f"refusing to pipe into a path containing '#': {text!r} — "
            f"tmux expands '#' sequences in a pipe-pane command"
        )
    # `exec` so the shell replaces itself with cat rather than lingering.
    return f"exec cat >> {shlex.quote(text)} 2>/dev/null"


def pane_is_piped(pane) -> bool:
    """Read tmux's own view of whether this pane has a pipe attached."""
    result = pane.cmd("display-message", "-p", "#{pane_pipe}")
    output = "".join(result.stdout).strip()
    return output == "1"


def attach_pipe_pane(session, fifo_path: Path | str) -> None:
    """Start piping a session's active pane into ``fifo_path``.

    **Plain ``-O``, never ``-o``.** ``man tmux``: "-o only opens a new pipe if
    no previous pipe exists, allowing a pipe to be toggled" -- and, separately,
    "any existing pipe is closed before shell-command is executed". So ``-o``
    against an already-piped pane is a *close*. It is also what the man page's
    own example uses, which is what makes it the thing that gets copied in.
    Re-attaching with ``-o`` would silently switch capture off and report
    success. Plain ``-O`` closes and reopens, which is the idempotence we want.

    **Never ``-I``.** That connects the command's stdout to the pane "as if it
    were typed" -- an injection route into a live agent, and squarely in Phase 0
    finding F2's territory. We only ever read.

    The result is verified rather than assumed, for the same reason: a tap that
    reports success and captures nothing is this project's signature bug shape.
    """
    pane = session.active_window.active_pane
    if pane is None:
        raise TerminalTapError("session has no active pane")

    pane.cmd("pipe-pane", "-O", pipe_pane_command(fifo_path))

    if not pane_is_piped(pane):
        raise TerminalTapError(
            f"pipe-pane reported success but #{{pane_pipe}} is not 1 for "
            f"session {session.name!r} — the tap is not capturing"
        )


def detach_pipe_pane(session) -> None:
    """Close a pane's pipe. ``pipe-pane`` with no command is the documented
    way to do that ("If no shell-command is given, the current pipe (if any)
    is closed")."""
    pane = session.active_window.active_pane
    if pane is not None:
        with contextlib.suppress(Exception):
            pane.cmd("pipe-pane")


def session_agent_id(session) -> str | None:
    """Read ``FLEETVIEW_AGENT_ID`` back out of a session's environment.

    libtmux passes ``environment=`` as ``-e KEY=VAL`` to ``new-session``, which
    sets the *session* environment, so this survives for the session's life and
    makes tmux itself the agent registry -- no state for FleetView to persist
    and no stale rows to reconcile.

    Deliberately not "assume the agent id is the session name". That happens to
    be true today only because ``fleetview spike`` passes the same string for
    both, and coincidences like that stop being true without warning.
    """
    try:
        result = session.cmd("show-environment", "FLEETVIEW_AGENT_ID")
        for line in result.stdout:
            line = line.strip()
            if line.startswith("FLEETVIEW_AGENT_ID="):
                value = line.split("=", 1)[1]
                return value or None
            if line == "-FLEETVIEW_AGENT_ID":
                return None  # tmux's spelling for "unset in this session"
    except Exception:
        return None
    return None


def find_agent_sessions(server: libtmux.Server | None = None) -> dict[str, str]:
    """Map ``agent_id -> session_name`` for every live FleetView agent.

    This is the registry. tmux was chosen (§4) precisely because sessions
    outlive the daemon, which means the fleet can be rediscovered after a
    restart rather than remembered across one.
    """
    srv = server if server is not None else libtmux.Server()
    found: dict[str, str] = {}
    try:
        sessions = srv.sessions
    except Exception:
        return found  # no tmux server running: no agents, not an error
    for session in sessions:
        agent_id = session_agent_id(session)
        if agent_id:
            found[agent_id] = session.name
    return found
