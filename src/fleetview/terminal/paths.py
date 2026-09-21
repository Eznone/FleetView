"""Where an agent's terminal bytes live on disk, and what may name them.

Every path FleetView writes under ``terminal_log_dir`` or ``fifo_dir`` is
derived here, so agent ids reach the filesystem through exactly one validator.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fleetview.config import Settings

#: Agent ids come from a spawn caller and end up in three places that each
#: punish a different character: a filesystem path, a tmux target, and the
#: ``pipe-pane`` shell command — where tmux expands the status-left sequences,
#: so a ``#`` in a filename silently becomes something else entirely
#: (``#I`` -> the window index). Rather than escape per destination, the id is
#: constrained to what is safe in all three.
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: seg-000007-20260921T140322Z.log
#:
#: Sequence *before* timestamp so a lexicographic sort is a chronological sort.
#: Sorting by timestamp would reorder the log across a DST change or an NTP
#: step backwards, which is exactly when someone is reading it.
_SEGMENT_RE = re.compile(r"^seg-(\d{6})-\d{8}T\d{6}Z\.log$")


class UnsafeAgentIdError(ValueError):
    """Raised when an agent id may not be turned into a path."""


def agent_slug(agent_id: str) -> str:
    """Validate an agent id for use as a path component and a tmux target.

    Deliberately a validator, not a sanitiser. Rewriting ``../../etc`` into
    something harmless would silently file an agent's bytes under a name that
    is not its id, and every later lookup by id would come back empty with
    nothing to explain why.
    """
    if not SLUG_RE.match(agent_id or ""):
        raise UnsafeAgentIdError(
            f"unsafe agent id {agent_id!r}: must match {SLUG_RE.pattern} "
            f"(no path separators, no '#', no leading dot, 1-64 chars)"
        )
    return agent_id


def agent_log_dir(settings: Settings, agent_id: str) -> Path:
    return settings.terminal_log_dir / agent_slug(agent_id)


def fifo_path(settings: Settings, agent_id: str) -> Path:
    return settings.fifo_dir / f"{agent_slug(agent_id)}.fifo"


def segment_name(sequence: int, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"seg-{sequence:06d}-{when.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}.log"


def segment_sequence(path: Path) -> int | None:
    """The sequence encoded in a segment filename, or None if it is not one."""
    match = _SEGMENT_RE.match(path.name)
    return int(match.group(1)) if match else None


def list_segments(directory: Path) -> list[Path]:
    """Every segment in ``directory``, oldest first.

    Anything that is not a segment filename is ignored rather than counted:
    the directory is FleetView's, but a stray editor swapfile must not become
    part of an agent's byte budget or, worse, a rotation casualty.
    """
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir() if p.is_file() and segment_sequence(p) is not None]
    return sorted(found, key=lambda p: p.name)


def total_bytes(directory: Path) -> int:
    """Bytes currently on disk for this agent. Seeds the writer's running
    total once at startup; never called on the write path."""
    total = 0
    for path in list_segments(directory):
        try:
            total += path.stat().st_size
        except OSError:
            continue  # vanished under us; the next prune will reconcile
    return total


def next_sequence(directory: Path) -> int:
    """Resume numbering from disk, so a daemon restart does not reuse a name
    and append two runs' bytes into one file."""
    segments = list_segments(directory)
    if not segments:
        return 0
    return (segment_sequence(segments[-1]) or 0) + 1


def open_segment(directory: Path, sequence: int, *, mode: int) -> Path:
    """Create the next segment file with its mode enforced before any bytes
    reach it. Enforced *before*, not after: a file briefly readable is a file
    that was readable."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / segment_name(sequence)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    os.close(fd)
    path.chmod(mode)
    return path
