"""Economic event calendar + blackout gate for the signal engine."""

from events.blackout import is_blackout
from events.calendar import Event, get_events

__all__ = ["Event", "get_events", "is_blackout"]
