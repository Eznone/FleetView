"""Persistence. All SQL in FleetView lives under this package (§4.1)."""

from fleetview.store.db import apply_schema, connect
from fleetview.store.events import EventStore

__all__ = ["EventStore", "apply_schema", "connect"]
