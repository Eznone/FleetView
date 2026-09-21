"""Reading a tmux pane's bytes out of a FIFO, without blocking the event loop.

``tmux pipe-pane`` runs ``cat >> <fifo>`` beside the pane. This is the other
end of that pipe.

**The EOF problem, solved by not having one.** Opening a FIFO for reading
normally blocks until a writer appears, and when the last writer exits the
reader sees EOF — after which every ``read`` returns empty forever and a naive
loop spins a core flat. The usual answer is a reopen state machine, which is
easy to write *slightly* wrong and expensive when you do.

So instead: open the read end ``O_RDONLY | O_NONBLOCK`` (which POSIX says
returns immediately even with no writer — that is what makes this usable from
an event loop), and then open a *second* descriptor on the same FIFO for
writing and simply hold it. With a writer permanently present the read end
never sees EOF at all, so tmux's ``cat`` can come and go as often as it likes
and there is nothing to reopen and nothing to spin on.

The keeper fd is load-bearing, not tidy-up. Removing it looks like a cleanup
and turns this class into a busy loop the moment a pane dies. Its one cost is
that EOF can no longer tell us the writer is gone — which is why liveness is
established by polling tmux instead (see
:class:`~fleetview.terminal.supervisor.TerminalSupervisor`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
from pathlib import Path
from typing import Callable

from fleetview.config import FILE_MODE

log = logging.getLogger(__name__)


class FifoReader:
    """Drains one FIFO into a callback, on the running event loop."""

    def __init__(
        self,
        path: Path,
        *,
        on_bytes: Callable[[bytes], None],
        read_size: int = 64 * 1024,
    ) -> None:
        self.path = path
        self._on_bytes = on_bytes
        self._read_size = read_size
        self._read_fd: int | None = None
        self._keeper_fd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.bytes_read = 0
        self.read_errors = 0

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)

        # A leftover from an unclean shutdown may still have an orphaned `cat`
        # on the far end; replacing it is what guarantees we own this one.
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        os.mkfifo(self.path, FILE_MODE)
        os.chmod(self.path, FILE_MODE)  # mkfifo's mode is umask-masked

        if not stat.S_ISFIFO(os.stat(self.path).st_mode):  # pragma: no cover
            raise OSError(f"{self.path} is not a FIFO")

        self._read_fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        # See the module docstring: this is what removes EOF from the picture.
        self._keeper_fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)

        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self._read_fd, self._drain)

    def stop(self, *, drain: bool = True) -> None:
        """Close down in an order that does not lose the tail of the stream.

        The reader is removed first so nothing races the final drain, and the
        FIFO is unlinked last so a restart cannot inherit this one with an
        orphaned writer still attached to it.
        """
        if self._loop is not None and self._read_fd is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self._read_fd)

        if drain:
            self._drain()

        if self._keeper_fd is not None:
            os.close(self._keeper_fd)
            self._keeper_fd = None

        # With the keeper gone the pipe can now report EOF, so one more pass
        # collects anything tmux wrote between the two.
        if drain:
            self._drain()

        if self._read_fd is not None:
            os.close(self._read_fd)
            self._read_fd = None

        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    # --- the hot path ------------------------------------------------------

    def _drain(self) -> None:
        """Read until the pipe is empty.

        Everything here is wrapped, because an exception raised inside an
        ``add_reader`` callback is swallowed by the loop's exception handler:
        the tap would go quiet permanently with nothing visible to say why.
        """
        if self._read_fd is None:
            return
        try:
            while True:
                try:
                    data = os.read(self._read_fd, self._read_size)
                except (BlockingIOError, InterruptedError):
                    return
                except OSError:
                    self.read_errors += 1
                    log.exception("terminal fifo read failed: %s", self.path)
                    return
                if not data:
                    return  # EOF: only reachable once the keeper fd is closed
                self.bytes_read += len(data)
                self._on_bytes(data)
        except Exception:  # pragma: no cover - defensive
            self.read_errors += 1
            log.exception("terminal fifo callback failed: %s", self.path)
