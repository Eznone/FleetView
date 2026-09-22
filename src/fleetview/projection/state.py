"""The read-side view models (PROJECT_PLAN.md §5.4).

These are what the canvas draws. §4.1 tier 3 is explicit that the in-memory
projection is **authoritative for the UI** and the database is durability only,
never the render path -- so this module is the shape the UI reads, and the
event log is merely where it can be rebuilt from.

Field names are snake_case in Python and camelCase on the wire, the same
contract :mod:`fleetview.schema.events` sets. One deliberate asymmetry to know
about: the *envelope* is aliased, but an event's ``payload`` is a free-form
dict whose keys stay snake_case. The UI has to model both spellings, so the
models here alias everything rather than leaving the consumer to guess which
half of a response it is looking at.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from fleetview.schema.events import AgentState

#: CAO's orchestration vocabulary (§5.5). ``assign`` is async -- the worker
#: calls back -- while ``handoff`` is synchronous and blocks the caller. The
#: distinction is why only one of them puts the sender into
#: ``waiting_on_subagent``.
Orchestration = Literal["assign", "handoff", "send_message"]


class _Wire(BaseModel):
    """camelCase on the wire, ``extra="forbid"`` so a typo is a 422."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class AgentView(_Wire):
    """One node on the canvas, folded from that agent's events."""

    agent_id: str
    #: The §5.3 taxonomy. "Why is this agent waiting?" is the question the tool
    #: exists to answer, so this is never collapsed to a bare `blocked`.
    state: AgentState = AgentState.IDLE

    run_id: str | None = None
    provider: str | None = None
    tmux_session: str | None = None

    #: The tool named by the last `PreToolUse` with no `PostToolUse` yet. Held
    #: with its span and start time because §5.3's `waiting_on_tool` is defined
    #: by *elapsed time*, not by any event -- see `FleetProjection.tick`.
    current_tool: str | None = None
    current_tool_span: str | None = None
    current_tool_started_at: datetime | None = None

    #: Rendered on the edge (§5.3, §7's Phase 2 gate).
    blocking_dependency_ids: list[str] = Field(default_factory=list)

    #: Where `rate_limited` came from, so `quota.recovered` can put the agent
    #: back rather than guessing `idle`. Parking and resuming is the whole of
    #: §8.5 requirement 2 -- FleetView never retries through a quota wall, so
    #: the state it returns to has to be remembered rather than re-derived.
    state_before_rate_limit: AgentState | None = None

    last_event_at: datetime | None = None
    event_count: int = 0
    terminal_attached: bool = False
    last_message: str | None = None

    #: Claude Code's own internal subagents, counted but deliberately NOT
    #: treated as delegated work. See `FleetProjection._subagent_stop`.
    subagent_count: int = 0
    terminated: bool = False

    #: Written by `fleetview demo seed`, never by a real agent. The UI shows a
    #: ribbon: seeded data sharing a store with real telemetry is only safe
    #: while it cannot be mistaken for a fleet.
    synthetic: bool = False


class FleetEdge(_Wire):
    """A delegation or message edge between two agents.

    Phase 2 renders these from seeded events; Phase 3's mailbox emits the same
    `message.inter_agent.*` types and this model does not change.
    """

    id: str
    source: str
    target: str
    orchestration: Orchestration = "send_message"
    #: `open` until a delivery or acknowledgement closes it. An open edge from
    #: an agent in `waiting_on_subagent` is the "blocked edge" §7 asks for.
    state: Literal["open", "closed"] = "open"
    conversation: str | None = None
    created_at: datetime | None = None


class FleetSnapshot(_Wire):
    """Everything the canvas needs to draw itself from cold."""

    run_id: str | None = None
    generated_at: datetime
    max_concurrent_agents: int
    agents: list[AgentView] = Field(default_factory=list)
    edges: list[FleetEdge] = Field(default_factory=list)
