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
    event_retention_days: int = 30

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

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        home = Path(env.get("FLEETVIEW_HOME", str(DEFAULT_HOME))).expanduser()
        cap = int(env.get("FLEETVIEW_MAX_CONCURRENT_AGENTS", DEFAULT_MAX_CONCURRENT_AGENTS))
        return cls(home=home, max_concurrent_active_agents=cap)

    def ensure_directories(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.terminal_log_dir.mkdir(parents=True, exist_ok=True)
