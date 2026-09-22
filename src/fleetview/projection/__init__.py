"""The CQRS read side (PROJECT_PLAN.md §5.4) -- what the canvas draws."""

from fleetview.projection.fleet import FleetProjection
from fleetview.projection.service import PROJECTION_TOPIC, ProjectionService
from fleetview.projection.state import AgentView, FleetEdge, FleetSnapshot

__all__ = [
    "PROJECTION_TOPIC",
    "AgentView",
    "FleetEdge",
    "FleetProjection",
    "FleetSnapshot",
    "ProjectionService",
]
