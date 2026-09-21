"""Path derivation and the permission invariant (§4.1 tier 2, §8.5 req 3).

Terminal bytes are unredacted and will contain sign-in URLs and any token an
agent echoes. Redaction is Phase 6; until then the mode bits are the mitigation,
so they are asserted rather than assumed.
"""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone

import pytest

from fleetview.config import DIR_MODE, FILE_MODE, Settings
from fleetview.terminal.paths import (
    UnsafeAgentIdError,
    agent_log_dir,
    agent_slug,
    fifo_path,
    list_segments,
    next_sequence,
    open_segment,
    segment_name,
    segment_sequence,
    total_bytes,
)


# --- the slug validator ------------------------------------------------------

@pytest.mark.parametrize(
    "agent_id",
    [
        "../../etc/passwd",   # traversal
        "a/b",                # separator
        "x#y",                # tmux expands '#' in the pipe-pane command string
        "",                   # empty
        ".hidden",            # leading dot
        "-leading-dash",      # could read as a flag at a tmux target
        "z" * 65,             # over length
        "has space",
        "tab\there",
        "nul\x00byte",
    ],
)
def test_unsafe_agent_ids_are_refused(agent_id):
    with pytest.raises(UnsafeAgentIdError):
        agent_slug(agent_id)


@pytest.mark.parametrize(
    "agent_id",
    ["fleetview-claude-a1b2c3", "worker_1", "a", "A.B-C_1", "z" * 64],
)
def test_ordinary_agent_ids_pass(agent_id):
    assert agent_slug(agent_id) == agent_id


def test_a_rejected_id_never_reaches_a_path(tmp_path):
    """The validator must guard every path constructor, not just one of them."""
    settings = Settings(home=tmp_path)
    for build in (agent_log_dir, fifo_path):
        with pytest.raises(UnsafeAgentIdError):
            build(settings, "../escape")


def test_fifos_live_outside_the_log_directory(tmp_path):
    """Size accounting scandirs the log dir and trusts every entry is a
    segment. A FIFO in there would be counted, and could be pruned."""
    settings = Settings(home=tmp_path)
    log_dir = agent_log_dir(settings, "agent-1")
    assert settings.fifo_dir not in log_dir.parents
    assert fifo_path(settings, "agent-1").parent != log_dir


# --- segment naming ----------------------------------------------------------

def test_segment_names_sort_chronologically_even_when_the_clock_goes_back():
    """Sequence precedes timestamp so lexicographic order is write order.

    Naming by timestamp alone reorders the log across a DST change or an NTP
    step backwards — precisely when someone is reading it to find out what
    happened."""
    later_wall_clock = datetime(2026, 9, 21, 14, 0, 0, tzinfo=timezone.utc)
    earlier = later_wall_clock - timedelta(hours=1)

    first = segment_name(1, later_wall_clock)   # written first
    second = segment_name(2, earlier)           # written second, clock stepped back

    assert sorted([second, first]) == [first, second]


def test_segment_sequence_round_trips():
    from pathlib import Path
    assert segment_sequence(Path(segment_name(42))) == 42


def test_non_segment_files_are_not_segments(tmp_path):
    """A stray file must not join the byte budget or become a rotation casualty."""
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / ".seg-000001-20260921T140000Z.log.swp").write_text("x")
    (tmp_path / segment_name(1)).write_bytes(b"real")

    assert [p.name for p in list_segments(tmp_path)] == [segment_name(1)]
    assert total_bytes(tmp_path) == 4


def test_next_sequence_resumes_from_disk(tmp_path):
    """A restart must not reuse a name and append two runs into one file."""
    assert next_sequence(tmp_path) == 0
    (tmp_path / segment_name(0)).write_bytes(b"")
    (tmp_path / segment_name(5)).write_bytes(b"")
    assert next_sequence(tmp_path) == 6


# --- the permission invariant ------------------------------------------------

def test_directories_are_private(tmp_path):
    settings = Settings(home=tmp_path / "fv")
    settings.ensure_directories()
    for directory in (settings.home, settings.terminal_log_dir, settings.fifo_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == DIR_MODE, directory


def test_existing_world_readable_directories_are_repaired(tmp_path):
    """mkdir(mode=) does nothing to a directory that already exists, so an
    install created before this code would stay world-readable forever."""
    home = tmp_path / "fv"
    (home / "terminal").mkdir(parents=True)
    home.chmod(0o755)
    (home / "terminal").chmod(0o755)

    Settings(home=home).ensure_directories()

    assert stat.S_IMODE(home.stat().st_mode) == DIR_MODE
    assert stat.S_IMODE((home / "terminal").stat().st_mode) == DIR_MODE


def test_segment_files_are_private_before_any_bytes_land(tmp_path):
    """Enforced at creation, not after the first write: a file briefly
    readable is a file that was readable."""
    path = open_segment(tmp_path / "agent-1", 0, mode=FILE_MODE)
    assert path.stat().st_size == 0
    assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE
    assert stat.S_IMODE(path.parent.stat().st_mode) == DIR_MODE


def test_open_segment_is_umask_independent(tmp_path):
    """os.open's mode is umask-masked; the explicit chmod is what makes this
    hold under a permissive umask."""
    import os
    old = os.umask(0o000)
    try:
        path = open_segment(tmp_path / "agent-2", 0, mode=FILE_MODE)
        assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE
    finally:
        os.umask(old)
