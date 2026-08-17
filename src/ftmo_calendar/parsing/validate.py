"""Convert raw LLM extractions into validated, timezone-aware TradingEvents."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ftmo_calendar.config import EventRules
from ftmo_calendar.models import EventType, SourcePost, TradingEvent
from ftmo_calendar.parsing.llm import RawEvent

logger = logging.getLogger(__name__)

_OFFSET = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2}):?(\d{2})?$")
_EXCERPT_LIMIT = 800


@dataclass(frozen=True)
class Rejection:
    raw: RawEvent
    reason: str


def _offset_tz(stated: str) -> timezone | None:
    m = _OFFSET.match(stated.strip())
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    hours, minutes = int(m.group(2)), int(m.group(3) or 0)
    if hours > 23 or minutes > 59:
        return None
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def build_description(post: SourcePost) -> str:
    excerpt = post.text[:_EXCERPT_LIMIT]
    if len(post.text) > _EXCERPT_LIMIT:
        excerpt += "…"
    return f"{excerpt}\n\nSource: {post.url}\nCreated by AutoFtmoCalendar"


def validate_events(
    raw_events: list[RawEvent],
    post: SourcePost,
    rules: EventRules,
    source_tz: ZoneInfo,
    calendar_tz: ZoneInfo,
    now: datetime | None = None,
    require_stated_offset: bool = False,
) -> tuple[list[TradingEvent], list[Rejection]]:
    """Normalize raw extractions into calendar events.

    `require_stated_offset` turns the timezone fallback off for firms whose
    platform clock matches no IANA zone (see SourceProfile). Such an event is
    rejected rather than published at a guessed offset.
    """
    now = now or datetime.now(UTC)
    events: list[TradingEvent] = []
    rejections: list[Rejection] = []

    for raw in raw_events:
        try:
            start = datetime.fromisoformat(raw.start_time)
            end = datetime.fromisoformat(raw.end_time)
        except ValueError as e:
            rejections.append(Rejection(raw, f"unparseable datetime: {e}"))
            continue

        stated = _offset_tz(raw.stated_utc_offset) if raw.stated_utc_offset else None
        naive = start.tzinfo is None or end.tzinfo is None
        if require_stated_offset and stated is None and naive:
            rejections.append(
                Rejection(
                    raw,
                    "no UTC offset stated in the announcement and this source has no "
                    "timezone that can be assumed — refusing to guess the hour",
                )
            )
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=stated or source_tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=stated or source_tz)

        if end <= start:
            rejections.append(Rejection(raw, "end is not after start"))
        elif end - start > timedelta(hours=rules.max_duration_hours):
            rejections.append(
                Rejection(raw, f"duration exceeds {rules.max_duration_hours}h sanity cap")
            )
        elif start > now + timedelta(days=rules.max_days_ahead):
            rejections.append(Rejection(raw, "too far in the future"))
        elif end <= now:
            rejections.append(Rejection(raw, "already ended"))
        elif raw.confidence == "low" and rules.reject_low_confidence:
            rejections.append(Rejection(raw, "low extraction confidence"))
        else:
            event_type = EventType(raw.event_type)
            low = raw.confidence == "low"
            if low:
                logger.info(
                    "Low-confidence extraction for %s (%s %s); publishing flagged",
                    post.post_key,
                    raw.event_type,
                    raw.start_time,
                )
            events.append(
                TradingEvent(
                    event_type=event_type,
                    summary=_build_summary(event_type, raw.affected, rules, low=low),
                    description=_describe(post, low=low),
                    start=start.astimezone(calendar_tz),
                    end=end.astimezone(calendar_tz),
                    source_post_key=post.post_key,
                    source_url=post.url,
                    confidence=raw.confidence,
                )
            )
    return events, rejections


_LOW_CONFIDENCE_NOTE = (
    "Extraction confidence: LOW — the announcement did not state this clearly. "
    "Check the source before relying on it."
)


def _describe(post: SourcePost, *, low: bool) -> str:
    description = build_description(post)
    return f"{_LOW_CONFIDENCE_NOTE}\n\n{description}" if low else description


_AFFECTED_LIMIT = 70


def _build_summary(
    event_type: EventType, affected: str | None, rules: EventRules, *, low: bool = False
) -> str:
    summary = rules.summaries.get(event_type.value, rules.summaries["other"])
    # The affected list is model-extracted from scraped text — strip control
    # characters so it can never smuggle line breaks into ICS/HTML contexts.
    affected = "".join(ch for ch in (affected or "") if ch.isprintable()).strip()
    if affected:
        if len(affected) > _AFFECTED_LIMIT:
            affected = affected[:_AFFECTED_LIMIT].rstrip(", ") + "…"
        summary = f"{summary} — {affected}"
    # A guess the model itself flagged must not sit in a subscriber's calendar
    # looking exactly as certain as a stated maintenance window.
    if low and rules.low_confidence_marker:
        summary = f"{summary} {rules.low_confidence_marker}"
    return summary
