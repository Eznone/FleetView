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

#: One spelling of "off" for every boolean environment variable, so
#: FLEETVIEW_UI=false and FLEETVIEW_TERMINAL_CAPTURE=0 do not disagree about
#: what counts as disabled.
_FALSY = {"0", "false", "no", "off"}


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

    # --- the live canvas (§5.4, §5.6) --------------------------------------

    #: Set false to run the daemon with no projection at all -- the Phase 1
    #: posture. The load gate uses it to isolate the ingest path, exactly as
    #: `terminal_capture_enabled` does for the terminal plane: F9 puts the
    #: ingest ceiling at ~1000 events/s with the gate sitting on it, so being
    #: able to measure one subsystem at a time is what keeps a regression
    #: attributable instead of merely visible.
    projection_enabled: bool = True

    #: §5.3 defines `waiting_on_tool` as "`PreToolUse` with no `PostToolUse`
    #: past threshold" -- a property of elapsed time that no event announces,
    #: so the projection has to look at the clock. Too low and every ordinary
    #: file read flashes amber; too high and a genuinely wedged tool call looks
    #: like an agent that is merely thinking.
    tool_stall_threshold_ms: int = 5000

    #: How much of the log the projection replays at startup. The daemon is
    #: one run (§3.3), so this only has to cover a restart mid-fleet, not the
    #: 30-day retention window -- replaying that would make startup wait on
    #: disk for a canvas the operator is already looking at.
    projection_replay_limit: int = 5000

    #: How often the projection's clock-driven transitions run. Off the ingest
    #: path, for the same reason §4.1 keeps pruning off it.
    projection_tick_ms: int = 250

    #: How often a changed fleet is broadcast, at most.
    #:
    #: Measured, not guessed. Publishing a snapshot per *event* cost the §7
    #: load gate ~9% of its throughput -- 907 events/s against a 950 floor --
    #: because building one means a deep copy of every view. F9 warns that the
    #: ingest ceiling is ~1000/s and the gate sits exactly on it, so anything
    #: added to this loop has to be paid for. Coalescing caps snapshot
    #: construction at 40/s however fast events arrive, and still leaves room
    #: under §7's 100 ms node-state budget beside the 50 ms group commit.
    projection_broadcast_ms: int = 25

    #: Depth of the *projection's* own bus subscription.
    #:
    #: Deeper than `ui_bus_queue_size` on purpose, and the reason is not
    #: performance. A drop on a UI client's queue is a stale frame that the
    #: next snapshot corrects. A drop here is an event the fold never sees --
    #: so an agent can sit in the wrong state until its next event, and a
    #: canvas that quietly disagrees with the fleet is worse than no canvas.
    #: These are object references, not bytes, so depth is nearly free.
    projection_bus_queue_size: int = 8192

    # --- the UI listener (§3.3, §4) ----------------------------------------

    #: Set false to run the daemon headless -- the Phase 1 posture, and what
    #: the load gate uses to measure ingest without a WebSocket fan-out on the
    #: same loop.
    ui_enabled: bool = True

    #: Loopback only, and *enforced* rather than merely defaulted -- see
    #: `fleetview.daemon.ui_app.check_ui_host`. The terminal plane serves
    #: unredacted pane bytes (see DIR_MODE above), so a bind beyond 127.0.0.1
    #: would put a sign-in URL an agent echoed onto the local network.
    ui_host: str = "127.0.0.1"
    ui_port: int = 8420

    #: Depth of a UI client's bus subscription.
    #:
    #: Deliberately *shallow*, and the exact mirror image of
    #: `terminal_bus_queue_size` below. There, a dropped item is a hole in a
    #: file whose selling point is being byte-for-byte authentic, so the queue
    #: is hours deep. Here, a dropped item is a stale frame in front of an
    #: operator who is about to get a fresher one, and the durable copy is in
    #: SQLite regardless. A UI that cannot keep up should fall behind and
    #: recover, never apply backpressure toward a hook shim holding a worker's
    #: tool call open (:mod:`fleetview.bus`).
    ui_bus_queue_size: int = 512

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
        ui = env.get("FLEETVIEW_UI", "1").strip().lower()
        projection = env.get("FLEETVIEW_PROJECTION", "1").strip().lower()
        return cls(
            home=home,
            max_concurrent_active_agents=cap,
            terminal_capture_enabled=capture not in _FALSY,
            projection_enabled=projection not in _FALSY,
            ui_enabled=ui not in _FALSY,
            ui_host=env.get("FLEETVIEW_UI_HOST", cls.ui_host),
            ui_port=int(env.get("FLEETVIEW_UI_PORT", cls.ui_port)),
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
