"""Sink protocol — what the pipeline needs from a calendar backend."""

from __future__ import annotations

from typing import Protocol

from prop_firm_calendar.models import TradingEvent


class EventSink(Protocol):
    def find_event_id_by_key(self, event_key: str) -> str | None: ...

    def create_event(self, event: TradingEvent) -> str:
        """Create the event; returns the backend's event id."""
        ...

    def delete_event(self, event_id: str) -> None: ...
