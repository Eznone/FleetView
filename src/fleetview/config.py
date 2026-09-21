"""Paths and settings for the daemon.

One place where "where does this live" and "how many agents may run" are
answered, so neither gets re-decided ad hoc further down the stack.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Everything FleetView writes lives under here. Pinned to the home directory,
#: on purpose: PROJECT_PLAN.md §4.1 requires the data directory be on ext4, and
#: on WSL2 anything under /mnt/c is a 9p mount where SQLite's POSIX locking is
#: unreliable. :mod:`fleetview.fsguard` enforces that at startup.
DEFAULT_HOME = Path("~/.fleetview")

#: Anthropic's published limits state that they "assume ordinary, individual
#: usage of Claude Code and the Agent SDK".
#:
#: That sentence is the reason this number is 3 rather than 15. It is not a
#: performance limit and not a guess at what the machine can handle — the store
#: has roughly 3000x headroom over the projected peak (§4.1). It is a
#: deliberate reading of a terms-of-service constraint, and per §8.5
#: requirement 1 it is raisable by the operator only with explicit
#: acknowledgement. If you are about to raise the default because it felt low,
#: re-read §8.4 first: the binding constraint on this project is scale, not
#: throughput, and enforcement can arrive without notice.
DEFAULT_MAX_CONCURRENT_AGENTS = 3

#: Terminal bytes are the one place a credential can appear as *content* rather
#: than as configuration: a sign-in URL, an agent echoing a token back, a `env`
#: run in a pane. FleetView never handles credentials (§8.5 requirement 3), but
#: it does now record what an agent's screen said, verbatim and unredacted.
#:
#: Redaction is Phase 6. Until then the mitigations are these two modes and the
#: retention cap, so they are not cosmetic — do not relax them to "fix" a
#: permission error. Both are asserted by test.
DIR_MODE = 0o700
FILE_MODE = 0o600


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. Built once at startup and passed down."""

    home: Path = field(default_factory=lambda: DEFAULT_HOME.expanduser())
    max_concurrent_active_agents: int = DEFAULT_MAX_CONCURRENT_AGENTS

    #: Group-commit window for the event writer (§4.1 tier 1). Events are
    #: batched for at most this long before hitting disk, so ingest never waits
    #: on fsync. Raising it trades liveness in the UI for fewer commits; there
    #: is no throughput reason to.
    flush_interval_ms: int = 50
    #: ...or this many events, whichever comes first. Bounds the batch under a
    #: burst so a single transaction stays small.
    flush_max_events: int = 500

    #: How long structured events are kept before pruning by runId (§4.1).
    #: Declared but not yet read: 30-day event pruning is unimplemented. The
    #: terminal plane's own retention below *is* enforced, by the writer.
    event_retention_days: int = 30

    # --- terminal plane (§4.1 tier 2) --------------------------------------
    #
    # Every number here is reasoned from §4.1's stated 50-500 KB/min per agent,
    # not measured against a real fleet. They are settings precisely so the
    # first long run can correct them.

    #: Set false to run the daemon with no terminal capture at all. The load
    #: test uses this to isolate the ingest path.
    terminal_capture_enabled: bool = True

    #: Rotate to a new segment file past this size. A single read is never
    #: split across segments, so a segment may overshoot by at most one read
    #: buffer — the threshold is a trigger, not a hard ceiling.
    terminal_segment_max_bytes: int = 16 * 1024 * 1024

    #: §4.1 retention: 48 h or 200 MB per agent, whichever comes first.
    #:
    #: Enforced by the *writer*, not by a sweeper (§9 risk 8). That is the
    #: whole point: a sweeper that dies leaves the disk filling silently, while
    #: a writer that cannot prune stops writing and says so. Channel B is the
    #: only component in FleetView that can fill a disk.
    terminal_max_bytes_per_agent: int = 200 * 1024 * 1024
    terminal_retention_hours: int = 48

    #: A terminal_chunks row is a *time index* into a contiguous file, not a
    #: container for bytes. One row covers this many bytes, or this much quiet,
    #: whichever comes first. Row-per-read would be §4.1's scaling mistake one
    #: level up: the bytes would stay out of the database while the *index*
    #: grew at the byte rate.
    terminal_chunk_bytes: int = 256 * 1024
    terminal_chunk_idle_ms: int = 1000

    terminal_read_buffer_bytes: int = 64 * 1024

    #: Depth of the LogWriter's bus subscription.
    #:
    #: Sized deliberately rather than inherited from the bus default of 1024.
    #: The bus drops the *oldest* item when a subscriber queue fills, and on
    #: this path a drop is a hole in a file whose selling point (§2.5) is being
    #: byte-for-byte authentic. At 500 KB/min and 64 KiB reads that is ~8
    #: items/min, so this depth is hours of slack: a drop here means the writer
    #: task is genuinely wedged, not merely behind. Drops are counted and
    #: surfaced as `terminal.gap` — see :mod:`fleetview.terminal.writer`.
    terminal_bus_queue_size: int = 4096

    #: How often the supervisor flushes idle chunk rows...
    terminal_tick_ms: int = 250
    #: ...and how often it reconciles its taps against live tmux sessions.
    terminal_reconcile_seconds: int = 5

    @property
    def db_path(self) -> Path:
        return self.home / "fleetview.db"

    @property
    def socket_path(self) -> Path:
        """Unix socket the hook shim writes to. The hook plane's transport."""
        return self.home / "daemon.sock"

    @property
    def terminal_log_dir(self) -> Path:
        """Tier 2 lives here as flat files — never in the database (§4.1)."""
        return self.home / "terminal"

    @property
    def fifo_dir(self) -> Path:
        """FIFOs live *outside* terminal_log_dir, deliberately.

        The writer's size accounting scandirs an agent's log directory and
        sums what it finds. Keeping the FIFO elsewhere means every entry in
        that directory is a segment file, so the sum is right by construction
        rather than by remembering to skip things.
        """
        return self.home / "run"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        home = Path(env.get("FLEETVIEW_HOME", str(DEFAULT_HOME))).expanduser()
        cap = int(env.get("FLEETVIEW_MAX_CONCURRENT_AGENTS", DEFAULT_MAX_CONCURRENT_AGENTS))
        capture = env.get("FLEETVIEW_TERMINAL_CAPTURE", "1").strip().lower()
        return cls(
            home=home,
            max_concurrent_active_agents=cap,
            terminal_capture_enabled=capture not in {"0", "false", "no", "off"},
        )

    def ensure_directories(self) -> None:
        """Create the data directories, and *enforce* their modes.

        The chmod is separate from the mkdir on purpose. ``mkdir(mode=...)`` is
        masked by the process umask, and does nothing at all to a directory
        that already exists — so passing a mode there would look like it worked
        while leaving an existing install world-readable forever. Doing it
        after, unconditionally, repairs installs created before this code.
        """
        for directory in (self.home, self.terminal_log_dir, self.fifo_dir):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(DIR_MODE)
