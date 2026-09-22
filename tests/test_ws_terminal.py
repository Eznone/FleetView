"""`/ws/terminal/{id}` -- the pane, byte for byte.

This socket follows the **file**, not the bus, and that is a decision rather
than an oversight. The `LogWriter` is itself a bus subscriber, so the file lags
the bus by an amount nothing bounds -- which means no ordering of "subscribe,
then replay from disk" closes the seam between them. Bytes published just
before the subscribe may not be on disk when the replay reads it, and they
would appear in neither stream. Following one source has no seam to get wrong,
reuses the read path `fleetview terminal tail` already proves, and keeps the
file authoritative exactly as §2.5 intends.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fleetview.terminal.paths import agent_log_dir, open_segment
from uiharness import BASE_URL, build, ws


def segment(settings, agent: str, sequence: int, data: bytes):
    directory = agent_log_dir(settings, agent)
    directory.mkdir(parents=True, exist_ok=True)
    path = open_segment(directory, sequence, mode=0o600)
    path.write_bytes(data)
    return path


def test_the_pane_is_replayed_with_its_escape_sequences_intact(tmp_path):
    """Raw ANSI is the point (§2.5). Stripping it would blank the pane."""
    ui, _hook, settings = build(tmp_path)
    segment(settings, "a1", 1, b"\x1b[32mhello\x1b[0m\r\n")

    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/terminal/a1") as socket:
            assert socket.receive_bytes() == b"\x1b[32mhello\x1b[0m\r\n"


def test_the_backfill_seam_neither_drops_nor_duplicates_bytes(tmp_path):
    """The one mechanic this socket exists to get right.

    Replay then follow, from a single source, with the cursor carried across
    the handover. A byte served twice corrupts a TUI redraw just as surely as
    a byte lost, so the assertion is on the exact concatenation.
    """
    ui, _hook, settings = build(tmp_path)
    path = segment(settings, "a1", 1, b"AAAA")

    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/terminal/a1") as socket:
            received = socket.receive_bytes()
            assert received == b"AAAA"

            with open(path, "ab") as handle:
                handle.write(b"BBBB")
            received += socket.receive_bytes()

            with open(path, "ab") as handle:
                handle.write(b"CCCC")
            received += socket.receive_bytes()

    assert received == b"AAAABBBBCCCC"


def test_capture_survives_a_rotation_underneath_the_reader(tmp_path):
    """The writer rotates without telling anyone reading the file.

    Following only the file that was open at connect time goes quiet at the
    rotation while the agent keeps talking -- and looks exactly like an idle
    agent, which is the failure this project is built to make visible.
    """
    ui, _hook, settings = build(tmp_path)
    first = segment(settings, "a1", 1, b"before")

    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/terminal/a1") as socket:
            assert socket.receive_bytes() == b"before"

            with open(first, "ab") as handle:
                handle.write(b"-last")
            segment(settings, "a1", 2, b"after")

            received = b""
            while b"after" not in received:
                received += socket.receive_bytes()

    assert received == b"-lastafter", "the tail of the old segment is not skipped"


def test_an_agent_with_no_capture_connects_and_waits(tmp_path):
    """A tap that has not opened yet is not an error -- the agent may be booting."""
    ui, _hook, settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/terminal/ghost") as socket:
            segment(settings, "ghost", 1, b"awake")
            assert socket.receive_bytes() == b"awake"


@pytest.mark.parametrize("agent", [".hidden", "bad!id", "a b", "-leading"])
def test_an_unsafe_agent_id_is_closed_not_served(tmp_path, agent):
    """`agent_slug` guards a filesystem path, a tmux target and a shell command.

    The characters that never survive a URL -- `#`, a bare `/` -- are covered
    in `test_terminal_paths`; these are the ones that reach the handler intact
    and would otherwise be handed straight to `agent_log_dir`.
    """
    ui, _hook, _settings = build(tmp_path)
    with TestClient(ui, base_url=BASE_URL) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with ws(client, f"/ws/terminal/{agent}"):
                pass
        assert caught.value.code == 1008


def test_a_pane_client_is_registered_and_released(tmp_path):
    ui, _hook, settings = build(tmp_path)
    segment(settings, "a1", 1, b"x")
    with TestClient(ui, base_url=BASE_URL) as client:
        with ws(client, "/ws/terminal/a1") as socket:
            socket.receive_bytes()
            assert ui.state.clients.count == 1
    assert ui.state.clients.count == 0
