"""`pipe-pane` mechanics.

The `-o` test below is the one a future session is most likely to "fix" back,
because `man tmux`'s own example uses `-o`. The citation is in the assertion
message on purpose.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from fleetview.spawn.tmux import (
    TerminalTapError,
    attach_pipe_pane,
    detach_pipe_pane,
    find_agent_sessions,
    pane_is_piped,
    pipe_pane_command,
    session_agent_id,
)


class FakeResult:
    def __init__(self, stdout): self.stdout = stdout


class FakePane:
    def __init__(self, piped="0"):
        self.calls: list[tuple] = []
        self._piped = piped

    def cmd(self, *args):
        self.calls.append(args)
        if args[0] == "display-message":
            return FakeResult([self._piped])
        if args[0] == "pipe-pane":
            self._piped = "0" if len(args) == 1 else "1"
        return FakeResult([])


class FakeWindow:
    def __init__(self, pane): self.active_pane = pane


class FakeSession:
    def __init__(self, pane, name="fleetview-claude-abc", env=None):
        self.active_window = FakeWindow(pane)
        self.name = name
        self._env = env
        self.calls: list[tuple] = []

    def cmd(self, *args):
        self.calls.append(args)
        if args[0] == "show-environment":
            if self._env is None:
                return FakeResult(["-FLEETVIEW_AGENT_ID"])
            return FakeResult([f"FLEETVIEW_AGENT_ID={self._env}"])
        return FakeResult([])


# --- the command string ------------------------------------------------------

def test_the_path_is_quoted_for_the_shell_tmux_runs():
    cmd = pipe_pane_command("/home/me/my agent/a.fifo")
    assert "'/home/me/my agent/a.fifo'" in cmd
    assert cmd.startswith("exec cat >>")


def test_a_hash_in_the_path_is_refused():
    """tmux expands status-left sequences in this string, so '#I' becomes the
    window index and the pipe quietly lands in a different file."""
    with pytest.raises(TerminalTapError, match="#"):
        pipe_pane_command("/tmp/agent#1.fifo")


def test_the_command_only_ever_reads():
    cmd = pipe_pane_command("/tmp/a.fifo")
    assert ">>" in cmd and "<" not in cmd


# --- the flags ---------------------------------------------------------------

def test_attach_uses_capital_O_and_never_lowercase_o():
    """man tmux 3.4: "-o only opens a new pipe if no previous pipe exists,
    allowing a pipe to be toggled" — and "any existing pipe is closed before
    shell-command is executed". So `-o` against an already-piped pane CLOSES
    it. The man page's own example uses `-o`, which is what makes this the
    likely regression."""
    pane = FakePane()
    attach_pipe_pane(FakeSession(pane), "/tmp/a.fifo")

    (pipe_call,) = [c for c in pane.calls if c[0] == "pipe-pane"]
    assert "-O" in pipe_call, "the read direction flag is required"
    assert "-o" not in pipe_call, (
        "-o is a TOGGLE: a re-attach would silently switch capture off and "
        "report success (man tmux 3.4, pipe-pane)"
    )


def test_attach_never_uses_dash_I():
    """-I writes the command's stdout into the pane 'as if it were typed' —
    an injection route into a live agent."""
    pane = FakePane()
    attach_pipe_pane(FakeSession(pane), "/tmp/a.fifo")
    (pipe_call,) = [c for c in pane.calls if c[0] == "pipe-pane"]
    assert "-I" not in pipe_call


def test_attaching_twice_leaves_the_pane_piped():
    """The `-o` trap, stated as behaviour rather than as argv."""
    pane = FakePane()
    session = FakeSession(pane)
    attach_pipe_pane(session, "/tmp/a.fifo")
    attach_pipe_pane(session, "/tmp/a.fifo")
    assert pane_is_piped(pane) is True


# --- verification ------------------------------------------------------------

def test_a_tap_that_does_not_take_is_an_error_not_a_success():
    """F2's shape: success reported, work not done."""
    class NeverPipes(FakePane):
        def cmd(self, *args):
            self.calls.append(args)
            if args[0] == "display-message":
                return FakeResult(["0"])
            return FakeResult([])

    pane = NeverPipes()
    with pytest.raises(TerminalTapError, match="not capturing"):
        attach_pipe_pane(FakeSession(pane), "/tmp/a.fifo")


def test_detach_closes_the_pipe_with_no_command():
    pane = FakePane(piped="1")
    detach_pipe_pane(FakeSession(pane))
    assert ("pipe-pane",) in pane.calls
    assert pane_is_piped(pane) is False


def test_attach_without_a_pane_is_an_error():
    session = FakeSession(None)
    with pytest.raises(TerminalTapError, match="no active pane"):
        attach_pipe_pane(session, "/tmp/a.fifo")


# --- the registry ------------------------------------------------------------

def test_the_agent_id_is_read_from_the_session_not_guessed_from_its_name():
    """They match today only because `fleetview spike` passes the same string
    for both, and coincidences like that stop being true without warning."""
    session = FakeSession(FakePane(), name="some-session-name", env="agent-42")
    assert session_agent_id(session) == "agent-42"


def test_a_session_without_the_marker_is_not_ours():
    assert session_agent_id(FakeSession(FakePane(), env=None)) is None


def test_find_agent_sessions_maps_id_to_session():
    class FakeServer:
        sessions = [
            FakeSession(FakePane(), name="s1", env="agent-1"),
            FakeSession(FakePane(), name="s2", env=None),        # not ours
            FakeSession(FakePane(), name="s3", env="agent-3"),
        ]
    assert find_agent_sessions(FakeServer()) == {"agent-1": "s1", "agent-3": "s3"}


def test_no_tmux_server_is_no_agents_not_an_error():
    class Broken:
        @property
        def sessions(self): raise RuntimeError("no server running")
    assert find_agent_sessions(Broken()) == {}


# --- against a real tmux -----------------------------------------------------

needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed"
)


@needs_tmux
def test_against_real_tmux(tmp_path):
    """The one thing the fakes cannot prove: that tmux actually accepts our
    argv, reports pane_pipe, and hands back the session environment."""
    import libtmux
    import os

    socket = f"fleetview-test-{os.getpid()}"
    server = libtmux.Server(socket_name=socket)
    fifo = tmp_path / "a.fifo"
    os.mkfifo(fifo)

    session = None
    try:
        session = server.new_session(
            session_name="fv-tap-test",
            attach=False,
            window_command="sh -c 'while true; do sleep 1; done'",
            environment={"FLEETVIEW_AGENT_ID": "agent-real"},
            x=220, y=50,
        )

        # The unverified assumption from planning: does -e round-trip?
        assert session_agent_id(session) == "agent-real"
        assert find_agent_sessions(server) == {"agent-real": "fv-tap-test"}

        # Reading from the FIFO so `cat` is not blocked on open.
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            attach_pipe_pane(session, fifo)
            assert pane_is_piped(session.active_window.active_pane) is True

            # And the trap, live: a second attach must NOT switch it off.
            attach_pipe_pane(session, fifo)
            assert pane_is_piped(session.active_window.active_pane) is True

            detach_pipe_pane(session)
            assert pane_is_piped(session.active_window.active_pane) is False
        finally:
            os.close(fd)
    finally:
        if session is not None:
            with __import__("contextlib").suppress(Exception):
                session.kill()
        with __import__("contextlib").suppress(Exception):
            server.kill()
