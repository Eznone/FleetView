"""Refusing to put the event store on a filesystem that cannot hold it.

PROJECT_PLAN.md §4.1: on WSL2, `/mnt/c` is a **9p** mount where SQLite's POSIX
locking is unreliable and fsync is glacial. The failure mode is not a clean
error — it is intermittent lock corruption under a database that appears to
work. So the daemon refuses to start rather than degrading quietly.

The lookup reads ``/proc/mounts`` directly rather than shelling out to
``findmnt``: it is a pure string operation, it needs no subprocess, and it can
be handed a mounts table in a test, which matters because the filesystems we
care about cannot be created on the machine running the suite.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

#: Filesystems the event store must never live on. 9p and drvfs are the two
#: faces of the Windows drive as mounted by WSL; cifs and smbfs are network
#: shares with the same locking problem.
SLOW_FILESYSTEMS = frozenset({"9p", "drvfs", "cifs", "smbfs"})

_PROC_MOUNTS = Path("/proc/mounts")


class SlowFilesystemError(RuntimeError):
    """The chosen data directory is on a filesystem SQLite cannot be trusted on."""


def parse_mounts(text: str) -> list[tuple[str, str]]:
    """Parse ``/proc/mounts`` into ``(mountpoint, fstype)`` pairs.

    Mount points are octal-escaped in this file — a path containing a space
    appears as ``\\040`` — so they are unescaped here. A mount point with a
    space in it is unusual but entirely legal, and silently mismatching one
    would make the guard read a nearby filesystem's type instead of the real
    one.
    """
    mounts: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mountpoint = fields[1].replace("\\040", " ").replace("\\011", "\t")
        mounts.append((mountpoint, fields[2]))
    return mounts


def _read_mounts() -> list[tuple[str, str]]:
    try:
        return parse_mounts(_PROC_MOUNTS.read_text())
    except OSError:
        # No /proc/mounts (macOS, a container without procfs). We cannot tell,
        # and refusing to start on "cannot tell" would be worse than allowing
        # it: the constraint this guard exists for is WSL-specific.
        return []


def filesystem_type(path: Path | str, mounts: list[tuple[str, str]] | None = None) -> str | None:
    """Return the filesystem type ``path`` sits on, or None if undeterminable.

    Matched by longest mount point, **by path component** — a plain string
    prefix would match ``/mnt/c`` against ``/mnt/cache`` and report the wrong
    filesystem for it.

    The path need not exist yet; the nearest existing ancestor is used, which is
    what makes this answerable before the data directory has been created.
    """
    table = _read_mounts() if mounts is None else mounts
    if not table:
        return None

    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()

    target = PurePosixPath(candidate)
    best: tuple[int, str] | None = None
    for mountpoint, fstype in table:
        mount = PurePosixPath(mountpoint)
        if target == mount or _is_within(target, mount):
            depth = len(mount.parts)
            if best is None or depth > best[0]:
                best = (depth, fstype)
    return None if best is None else best[1]


def _is_within(target: PurePosixPath, mount: PurePosixPath) -> bool:
    try:
        return target.is_relative_to(mount)
    except ValueError:  # pragma: no cover - defensive
        return False


def require_fast_filesystem(path: Path | str, *, mounts: list[tuple[str, str]] | None = None) -> None:
    """Raise :class:`SlowFilesystemError` if ``path`` is on a slow filesystem.

    Called before the daemon opens the database, so the operator gets an error
    naming the reason instead of intermittent corruption weeks later.
    """
    fstype = filesystem_type(path, mounts)
    if fstype in SLOW_FILESYSTEMS:
        raise SlowFilesystemError(
            f"refusing to use {path} for FleetView's data directory: it is on a {fstype!r} "
            f"filesystem, where SQLite's file locking is unreliable and fsync is extremely slow. "
            f"On WSL2 this means a path under /mnt/c. Move the data directory onto ext4 — "
            f"~/.fleetview/ is the default — or set FLEETVIEW_HOME to a path on the Linux "
            f"filesystem. See PROJECT_PLAN.md §4.1."
        )
