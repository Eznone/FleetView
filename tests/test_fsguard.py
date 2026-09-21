"""The data directory must never land on a filesystem SQLite cannot lock.

These use an injected mounts table rather than the real one, because the
filesystems that matter (9p, drvfs) cannot be created on the machine running
the suite — and a guard that is only exercised on WSL is a guard that rots.
"""

import pytest

from fleetview.fsguard import (
    SlowFilesystemError,
    filesystem_type,
    parse_mounts,
    require_fast_filesystem,
)

# A realistic WSL2 mounts table: ext4 root, the Windows drive on 9p, and a
# deliberate near-miss neighbour (/mnt/cache) to catch prefix bugs.
WSL_MOUNTS = [
    ("/", "ext4"),
    ("/mnt/wsl", "tmpfs"),
    ("/mnt/c", "9p"),
    ("/mnt/cache", "ext4"),
    ("/run/user/1000", "tmpfs"),
]


def test_parse_mounts_reads_proc_format():
    text = "/dev/sdc / ext4 rw,relatime 0 0\ndrvfs /mnt/c 9p rw,noatime 0 0\n"
    assert parse_mounts(text) == [("/", "ext4"), ("/mnt/c", "9p")]


def test_parse_mounts_unescapes_octal_in_mountpoints():
    """A space in a mount point is legal and appears as \\040. Mismatching it
    would silently read a *different* filesystem's type for that path."""
    assert parse_mounts("x /mnt/My\\040Drive cifs rw 0 0") == [("/mnt/My Drive", "cifs")]


def test_parse_mounts_ignores_malformed_lines():
    assert parse_mounts("\ngarbage\n/dev/sdc / ext4 rw 0 0\n") == [("/", "ext4")]


def test_longest_mountpoint_wins():
    assert filesystem_type("/mnt/c/Users/me/project", WSL_MOUNTS) == "9p"
    assert filesystem_type("/home/me/.fleetview", WSL_MOUNTS) == "ext4"


def test_prefix_match_is_component_wise_not_string_wise():
    """/mnt/cache starts with the string /mnt/c but is a different mount. A
    naive startswith() reports 9p here and refuses a perfectly good directory."""
    assert filesystem_type("/mnt/cache/fleetview", WSL_MOUNTS) == "ext4"


def test_mountpoint_itself_matches():
    assert filesystem_type("/mnt/c", WSL_MOUNTS) == "9p"


def test_unknown_when_no_mount_table():
    assert filesystem_type("/anywhere", []) is None


@pytest.mark.parametrize("fstype", ["9p", "drvfs", "cifs", "smbfs"])
def test_slow_filesystems_are_refused(fstype):
    with pytest.raises(SlowFilesystemError) as exc:
        require_fast_filesystem("/mnt/c/fleetview", mounts=[("/", "ext4"), ("/mnt/c", fstype)])
    message = str(exc.value)
    assert fstype in message, "the error must name the filesystem it found"
    assert "PROJECT_PLAN.md §4.1" in message, "and point at the reasoning"


def test_ext4_is_allowed():
    require_fast_filesystem("/home/me/.fleetview", mounts=WSL_MOUNTS)


def test_undeterminable_filesystem_is_allowed():
    """No /proc/mounts means we cannot tell. Refusing on "cannot tell" would
    break macOS and containers for a constraint that is WSL-specific."""
    require_fast_filesystem("/home/me/.fleetview", mounts=[])
