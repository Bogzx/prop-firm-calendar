"""Core data types shared across the pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EventType(StrEnum):
    MAINTENANCE = "maintenance"
    CRYPTO_CLOSURE = "crypto_closure"
    HOLIDAY_CLOSURE = "holiday_closure"  # symbol(s) closed the whole day
    EARLY_CLOSE = "early_close"  # session ends early on a date
    LATE_OPEN = "late_open"  # session starts late on a date
    SYMBOL_EVENT = "symbol_event"  # forced position closures, corporate actions
    OTHER = "other"
    HOLIDAY_HOURS = "holiday_hours"  # legacy (pre-0.8 state files); no longer produced


@dataclass(frozen=True)
class SourcePost:
    """One announcement post extracted from a source."""

    post_key: str
    title: str
    text: str
    url: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TradingEvent:
    """A normalized, calendar-ready event."""

    event_type: EventType
    summary: str
    description: str
    start: datetime  # timezone-aware
    end: datetime  # timezone-aware
    source_post_key: str
    source_url: str
    #: How sure the extraction was ("high" | "low"), carried through from the
    #: model's own assessment. Deliberately excluded from event_key: the same
    #: window re-extracted with different confidence is the same event, and a
    #: confidence flicker must not orphan a calendar entry.
    confidence: str = "high"

    @property
    def event_key(self) -> str:
        """Stable identity used for dedup and reconcile, stored on the Google event."""
        raw = "|".join(
            (
                self.source_post_key,
                self.event_type.value,
                self.start.isoformat(),
                self.end.isoformat(),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
