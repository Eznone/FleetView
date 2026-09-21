"""The FIFO reader.

The load-bearing property here is the keeper fd: with a writer permanently
held open the read end never sees EOF, so there is no reopen state machine and
no busy loop when a pane dies. Removing it looks like a cleanup, which is
exactly why it is tested rather than commented.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess

import pytest

from fleetview.config import FILE_MODE
from fleetview.terminal.fifo import FifoReader


async def _settle(rounds: int = 50) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def _reader(tmp_path, sink):
    return FifoReader(tmp_path / "run" / "a.fifo", on_bytes=sink.append)


async def test_start_creates_a_private_fifo(tmp_path):
    reader = _reader(tmp_path, [])
    reader.start()
    try:
        mode = os.stat(reader.path).st_mode
        assert stat.S_ISFIFO(mode)
        assert stat.S_IMODE(mode) == FILE_MODE
        assert stat.S_IMODE(reader.path.parent.stat().st_mode) == 0o700
    finally:
        reader.stop()


async def test_start_replaces_a_stale_fifo(tmp_path):
    """An unclean shutdown can leave one behind with an orphaned cat attached.

    Evidenced by the mode rather than the inode: a freed inode number is
    reused immediately, so comparing inodes passes or fails by luck.
    """
    path = tmp_path / "run" / "a.fifo"
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o666)
    os.chmod(path, 0o666)   # mkfifo's mode is umask-masked -- the same reason
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o666   # the code chmods too

    reader = _reader(tmp_path, [])
    reader.start()
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == FILE_MODE
        assert stat.S_ISFIFO(os.stat(path).st_mode)
    finally:
        reader.stop()


async def test_bytes_written_by_an_external_writer_arrive(tmp_path):
    sink: list[bytes] = []
    reader = _reader(tmp_path, sink)
    reader.start()
    try:
        subprocess.run(
            ["sh", "-c", f"printf 'hello from the pane' >> {reader.path}"],
            check=True, timeout=10,
        )
        await _settle()
        assert b"".join(sink) == b"hello from the pane"
        assert reader.bytes_read == 19
    finally:
        reader.stop()


async def test_a_writer_exiting_produces_no_eof_and_no_spin(tmp_path):
    """The keeper-fd property, stated as a test.

    Without the keeper this is where the reader would see EOF, start returning
    b"" on every ready-callback, and peg a core — while looking fine.
    """
    sink: list[bytes] = []
    reader = _reader(tmp_path, sink)
    reader.start()
    try:
        for i in range(3):
            subprocess.run(
                ["sh", "-c", f"printf 'writer-{i};' >> {reader.path}"],
                check=True, timeout=10,
            )
            await _settle()

        # The reader survived all three writers coming and going...
        assert b"".join(sink) == b"writer-0;writer-1;writer-2;"
        # ...and never took an error doing it.
        assert reader.read_errors == 0
        # The keeper is what makes that true.
        assert reader._keeper_fd is not None

        # A fourth writer still works, which is the actual requirement: a
        # re-attached pipe-pane must not need the reader restarted.
        subprocess.run(["sh", "-c", f"printf 'fourth' >> {reader.path}"],
                       check=True, timeout=10)
        await _settle()
        assert b"".join(sink).endswith(b"fourth")
    finally:
        reader.stop()


async def test_stop_drains_what_is_still_in_the_pipe(tmp_path):
    sink: list[bytes] = []
    reader = _reader(tmp_path, sink)
    reader.start()
    subprocess.run(["sh", "-c", f"printf 'tail bytes' >> {reader.path}"],
                   check=True, timeout=10)
    # Deliberately no settle: the bytes are in the pipe, unread.
    reader.stop()
    assert b"".join(sink) == b"tail bytes"


async def test_stop_unlinks_the_fifo(tmp_path):
    """Unlinked last, so a restart cannot inherit one with an orphaned writer."""
    reader = _reader(tmp_path, [])
    reader.start()
    path = reader.path
    assert path.exists()
    reader.stop()
    assert not path.exists()


async def test_stop_is_safe_when_the_fifo_already_vanished(tmp_path):
    reader = _reader(tmp_path, [])
    reader.start()
    reader.path.unlink()
    reader.stop()          # must not raise


async def test_a_failing_callback_does_not_kill_the_reader(tmp_path):
    """An exception inside an add_reader callback is swallowed by the loop, so
    the tap would go quiet forever with nothing visible to say why."""
    calls: list[bytes] = []

    def explode(data: bytes) -> None:
        calls.append(data)
        raise RuntimeError("downstream is broken")

    reader = FifoReader(tmp_path / "run" / "a.fifo", on_bytes=explode)
    reader.start()
    try:
        subprocess.run(["sh", "-c", f"printf 'first' >> {reader.path}"],
                       check=True, timeout=10)
        await _settle()
        assert reader.read_errors == 1

        # Still alive and still reading.
        subprocess.run(["sh", "-c", f"printf 'second' >> {reader.path}"],
                       check=True, timeout=10)
        await _settle()
        assert len(calls) == 2
    finally:
        reader.stop()


async def test_large_writes_are_reassembled_in_order(tmp_path):
    """A pane repainting can emit more than one read buffer at a time."""
    sink: list[bytes] = []
    reader = FifoReader(tmp_path / "run" / "a.fifo", on_bytes=sink.append,
                        read_size=4096)
    reader.start()
    try:
        payload = bytes(range(256)) * 256          # 64 KiB
        proc = subprocess.Popen(["sh", "-c", f"cat >> {reader.path}"],
                                stdin=subprocess.PIPE)
        proc.stdin.write(payload)
        proc.stdin.close()
        for _ in range(400):
            await asyncio.sleep(0.001)
            if sum(len(b) for b in sink) >= len(payload):
                break
        proc.wait(timeout=10)
        assert b"".join(sink) == payload
    finally:
        reader.stop()
