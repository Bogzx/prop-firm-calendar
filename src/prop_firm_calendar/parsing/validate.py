"""Convert raw LLM extractions into validated, timezone-aware TradingEvents."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from prop_firm_calendar.config import EventRules
from prop_firm_calendar.models import EventType, SourcePost, TradingEvent
from prop_firm_calendar.parsing.llm import RawEvent

logger = logging.getLogger(__name__)

_OFFSET = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2}):?(\d{2})?$")
_EXCERPT_LIMIT = 800
_WORD = re.compile(r"\w+")
_EVIDENCE_LIMIT = 300
#: Fewer words than this is not evidence of anything ("11:45 CT", "Nov 26").
_EVIDENCE_MIN_WORDS = 4


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(unicodedata.normalize("NFKC", text))]


def evidence_supported(quote: str, text: str) -> bool:
    """Does `quote` occur in `text`, word for word?

    Compared as word sequences, not raw strings: the scraped text joins table
    cells and elements with single spaces, and a model quoting a table row
    naturally adds pipes, colons or line breaks between the same words. What
    must survive is every word, in order, contiguously — a paraphrase or an
    invented sentence does not.
    """
    wanted = _words(quote)
    if len(wanted) < _EVIDENCE_MIN_WORDS:
        return False
    return f" {' '.join(wanted)} " in f" {' '.join(_words(text))} "


@dataclass(frozen=True)
class Rejection:
    raw: RawEvent
    reason: str
    #: The event is fine but not yet publishable (beyond `max_days_ahead`).
    #: The pipeline keeps it and re-validates it on later runs instead of
    #: forgetting it — an unchanged post is never sent to the LLM again, so a
    #: dropped far-future event would otherwise never appear at all.
    retryable: bool = False
    #: Expected in normal operation and not worth a human's attention: a window
    #: that has already passed, or a guess dropped because the operator asked
    #: for `reject_low_confidence`. Every other rejection means the
    #: announcement holds an event the calendar will not show.
    benign: bool = False


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
    return f"{excerpt}\n\nSource: {post.url}\nCreated by prop-firm-calendar"


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
            rejections.append(Rejection(raw, "too far in the future", retryable=True))
        elif end <= now:
            rejections.append(Rejection(raw, "already ended", benign=True))
        elif raw.confidence == "low" and rules.reject_low_confidence:
            rejections.append(Rejection(raw, "low extraction confidence", benign=True))
        elif (problem := _evidence_problem(raw, post)) and rules.require_evidence:
            rejections.append(Rejection(raw, problem))
        else:
            event_type = EventType(raw.event_type)
            evidence = _clean_evidence(raw.evidence) if not problem else ""
            # A quote the announcement does not contain is the model's own
            # words presented as the firm's: publish flagged, never as certain.
            fabricated = bool(raw.evidence) and problem is not None
            if fabricated:
                logger.warning(
                    "Evidence for %s %s on %s is not in the announcement; publishing as "
                    "low confidence",
                    raw.event_type,
                    raw.start_time,
                    post.post_key,
                )
            low = raw.confidence == "low" or fabricated
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
                    description=_describe(post, low=low, evidence=evidence),
                    start=start.astimezone(calendar_tz),
                    end=end.astimezone(calendar_tz),
                    source_post_key=post.post_key,
                    source_url=post.url,
                    confidence="low" if low else raw.confidence,
                    evidence=evidence,
                )
            )
    return events, rejections


_LOW_CONFIDENCE_NOTE = (
    "Extraction confidence: LOW — the announcement did not state this clearly. "
    "Check the source before relying on it."
)


def _describe(post: SourcePost, *, low: bool, evidence: str = "") -> str:
    description = build_description(post)
    if evidence:
        description = f"Announcement: \u201c{evidence}\u201d\n\n{description}"
    return f"{_LOW_CONFIDENCE_NOTE}\n\n{description}" if low else description


def _evidence_problem(raw: RawEvent, post: SourcePost) -> str | None:
    """Why this event's evidence cannot be trusted, or None when it can.

    A post re-validated after it left the index page (deferred events) has no
    text to check against; its quote was taken from the real text when it was
    extracted, so it is accepted as is.
    """
    if not raw.evidence or not raw.evidence.strip():
        return "no evidence quoted from the announcement"
    if post.text and not evidence_supported(raw.evidence, post.text):
        return "quoted evidence does not appear in the announcement"
    return None


def _clean_evidence(quote: str | None) -> str:
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in quote or "").split())
    return text[:_EVIDENCE_LIMIT].rstrip() + ("…" if len(text) > _EVIDENCE_LIMIT else "")


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
