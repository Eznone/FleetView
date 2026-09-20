"""Creating the detached tmux session an agent CLI runs inside.

tmux rather than a direct PTY, for three reasons (PROJECT_PLAN.md §4):
sessions survive daemon *and* UI restarts, the operator can ``tmux attach``
directly at any time, and ``pipe-pane`` gives a clean byte tap for the terminal
plane later.
"""

from __future__ import annotations

import shlex

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
    """Read recent pane output — the crude status check the spike needs."""
    pane = session.active_window.active_pane
    if pane is None:
        return ""
    return "\n".join(pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout)
