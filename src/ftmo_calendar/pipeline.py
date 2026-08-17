"""Orchestration: fetch → cache-check → extract → validate → reconcile."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from ftmo_calendar.config import AppConfig, EventRules
from ftmo_calendar.models import SourcePost, TradingEvent
from ftmo_calendar.parsing.llm import RawEvent
from ftmo_calendar.parsing.validate import validate_events
from ftmo_calendar.sinks.base import EventSink
from ftmo_calendar.state import PostState, State, TrackedEvent

logger = logging.getLogger(__name__)


class Source(Protocol):
    def fetch(self) -> list[SourcePost]: ...


class Extractor(Protocol):
    def extract(self, text: str) -> list[RawEvent]: ...


@dataclass
class RunReport:
    posts_seen: int = 0
    posts_relevant: int = 0
    posts_skipped_unchanged: int = 0
    events_created: int = 0
    events_deleted: int = 0
    events_kept: int = 0
    rejections: int = 0
    dry_run: bool = False
    created_lines: list[str] = field(default_factory=list)
    deleted_lines: list[str] = field(default_factory=list)
    #: Suspicious outcomes from a run that did not raise. A run can succeed
    #: mechanically and still be wrong — the keyword gate matching nothing, or
    #: extraction losing events a post used to have with none new to replace
    #: them. Callers
    #: surface these on /healthz and over the notification channels so a
    #: quietly broken sync cannot pass for a working one.
    anomalies: list[str] = field(default_factory=list)

    def summary(self) -> str:
        prefix = "[dry-run] " if self.dry_run else ""
        text = (
            f"{prefix}posts: {self.posts_seen} seen, {self.posts_relevant} relevant, "
            f"{self.posts_skipped_unchanged} unchanged | events: {self.events_created} created, "
            f"{self.events_deleted} removed, {self.events_kept} kept, "
            f"{self.rejections} rejected extractions"
        )
        if self.anomalies:
            text += f" | {len(self.anomalies)} anomaly/anomalies: " + "; ".join(self.anomalies)
        return text


def run_pipeline(
    *,
    source: Source,
    extractor: Extractor,
    sink: EventSink,
    state: State,
    config: AppConfig,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RunReport:
    now = now or datetime.now(UTC)
    report = RunReport(dry_run=dry_run)
    source_tz = ZoneInfo(config.source.timezone)
    calendar_tz = ZoneInfo(config.calendar.timezone)

    posts = source.fetch()
    report.posts_seen = len(posts)

    for post in posts:
        post_state = state.posts.get(post.post_key)
        if post_state is not None and not dry_run:
            post_state.last_seen = now.isoformat()

        if not _is_relevant(post, config.source.keywords):
            logger.info("Post %s has no relevant keywords; skipping", post.post_key)
            continue
        report.posts_relevant += 1

        if post_state is not None and post_state.content_hash == post.content_hash:
            logger.info("Post %s unchanged; skipping LLM call", post.post_key)
            report.posts_skipped_unchanged += 1
            continue

        logger.info("Post %s is new or changed; extracting events", post.post_key)
        raw_events = extractor.extract(post.text)
        events, rejections = validate_events(
            raw_events, post, config.events, source_tz, calendar_tz, now=now
        )
        report.rejections += len(rejections)
        for rejection in rejections:
            logger.warning("Rejected extraction for %s: %s", post.post_key, rejection.reason)

        new_post_state = _reconcile(
            post, events, post_state, sink, report, dry_run, now, config.events
        )
        if not dry_run:
            state.posts[post.post_key] = new_post_state

    _detect_anomalies(report, config.source.keywords)

    if not dry_run:
        state.prune(now=now)
    return report


def _detect_anomalies(report: RunReport, keywords: tuple[str, ...]) -> None:
    """Flag whole-run outcomes that mean the source moved rather than went quiet.

    Posts exist but none of them matched a keyword: either FTMO reworded (the
    gate looks for "maintenance", so an announcement titled "scheduled
    downtime" passes straight through) or the scraper is now reading the wrong
    part of a redesigned page. Both empty the calendar while every step
    reports success.
    """
    if report.posts_seen > 0 and report.posts_relevant == 0:
        message = (
            f"keyword gate matched none of {report.posts_seen} scraped post(s) — "
            f"the announcement wording or the page structure may have changed "
            f"(keywords: {', '.join(keywords)})"
        )
        logger.error("%s", message)
        report.anomalies.append(message)


def _describe_event(event: TradingEvent) -> str:
    return f"{event.summary} — {event.start:%a %d %b %H:%M}–{event.end:%H:%M %Z}"


def _describe_tracked(tracked: TrackedEvent) -> str:
    label = tracked.summary or tracked.event_key
    when = tracked.start or tracked.end
    with contextlib.suppress(ValueError):
        when = f"{datetime.fromisoformat(when):%a %d %b %H:%M}"
    return f"{label} — {when}"


def _track(event: TradingEvent, backend_id: str) -> TrackedEvent:
    return TrackedEvent(
        event_key=event.event_key,
        google_event_id=backend_id,
        end=event.end.isoformat(),
        summary=event.summary,
        start=event.start.isoformat(),
        event_type=event.event_type.value,
    )


def _is_relevant(post: SourcePost, keywords: tuple[str, ...]) -> bool:
    text = post.text.lower()
    return any(k.strip().lower() in text for k in keywords if k.strip())


def _future(tracked: TrackedEvent, now: datetime) -> bool:
    """True when a tracked event has not ended yet (naive timestamps read as UTC)."""
    end_dt = datetime.fromisoformat(tracked.end)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=UTC)
    return end_dt > now


def _reconcile(
    post: SourcePost,
    events: list[TradingEvent],
    post_state: PostState | None,
    sink: EventSink,
    report: RunReport,
    dry_run: bool,
    now: datetime,
    rules: EventRules,
) -> PostState:
    old = {e.event_key: e for e in (post_state.events if post_state else [])}
    new_keys = {e.event_key for e in events}
    tracked: list[TrackedEvent] = []

    # A post that produced events now produces fewer, with nothing new to
    # replace them — whether it collapsed to zero or merely shrank to a subset
    # (8 events becoming 1 is a degraded extraction, not seven withdrawals). A
    # genuine withdrawal looks identical to a degraded extraction (a typo fix
    # that confused the model, a truncated fetch, a consensus flicker, a prompt
    # regression) — and the degraded case is far more likely. A genuine
    # reschedule, by contrast, announces *new* times and passes this guard.
    # Deleting is irreversible for subscribers who have already planned around
    # the window, so keep every tracked event and raise an anomaly for a human
    # to judge. Set [events] delete_on_empty_extraction = true to restore the
    # old behavior.
    pending = [e for k, e in old.items() if k not in new_keys and _future(e, now)]
    if pending and not (new_keys - old.keys()) and not rules.delete_on_empty_extraction:
        message = (
            f"post {post.post_key} previously extracted {len(old)} event(s) and now extracts "
            f"{len(events)} with none new — refusing to delete {len(pending)} future event(s); "
            "verify the announcement was really withdrawn"
        )
        logger.error("%s", message)
        report.anomalies.append(message)
        report.events_kept += len(old)
        return PostState(
            content_hash=post.content_hash, last_seen=now.isoformat(), events=list(old.values())
        )

    for key, old_event in old.items():
        if key in new_keys:
            continue
        if not _future(old_event, now):
            tracked.append(old_event)  # it happened; preserve calendar history
            continue
        logger.info("Announcement changed: removing stale event %s", key)
        if not dry_run:
            sink.delete_event(old_event.google_event_id)
        report.events_deleted += 1
        report.deleted_lines.append(_describe_tracked(old_event))

    for event in events:
        if event.event_key in old:
            tracked.append(old[event.event_key])
            report.events_kept += 1
            continue
        if dry_run:
            logger.info("[dry-run] would create '%s' at %s", event.summary, event.start)
            report.events_created += 1
            report.created_lines.append(_describe_event(event))
            continue
        existing_id = sink.find_event_id_by_key(event.event_key)
        if existing_id:
            logger.info("Event %s already in calendar; adopting it", event.event_key)
            tracked.append(_track(event, existing_id))
            report.events_kept += 1
            continue
        google_id = sink.create_event(event)
        tracked.append(_track(event, google_id))
        report.events_created += 1
        report.created_lines.append(_describe_event(event))

    return PostState(content_hash=post.content_hash, last_seen=now.isoformat(), events=tracked)
