"""Orchestration: fetch → cache-check → extract → validate → reconcile."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from prop_firm_calendar.config import AppConfig, EventRules
from prop_firm_calendar.models import SourcePost, TradingEvent
from prop_firm_calendar.parsing.llm import RawEvent
from prop_firm_calendar.parsing.validate import Rejection, validate_events
from prop_firm_calendar.sinks.base import EventSink
from prop_firm_calendar.state import PostState, State, TrackedEvent

logger = logging.getLogger(__name__)


class Source(Protocol):
    def fetch(self) -> list[SourcePost]: ...


class Extractor(Protocol):
    def extract(self, text: str) -> list[RawEvent]: ...


@dataclass
class RunReport:
    #: Profile name of the firm this report covers ("" for the legacy caller
    #: that did not say). Used to label anomalies and per-firm health.
    firm: str = ""
    display_name: str = ""
    posts_seen: int = 0
    posts_relevant: int = 0
    posts_skipped_unchanged: int = 0
    events_created: int = 0
    events_deleted: int = 0
    events_kept: int = 0
    rejections: int = 0
    #: Extractions held back for being beyond max_days_ahead; see
    #: PostState.deferred.
    events_deferred: int = 0
    #: Duplicate calendar entries for one window collapsed into a shared one.
    duplicates_merged: int = 0
    #: One line per rejection that dropped a real event (not benign ones).
    rejected_lines: list[str] = field(default_factory=list)
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

    @property
    def label(self) -> str:
        return self.display_name or self.firm or "source"

    def summary(self) -> str:
        prefix = "[dry-run] " if self.dry_run else ""
        if self.firm:
            prefix += f"{self.label}: "
        text = (
            f"{prefix}posts: {self.posts_seen} seen, {self.posts_relevant} relevant, "
            f"{self.posts_skipped_unchanged} unchanged | events: {self.events_created} created, "
            f"{self.events_deleted} removed, {self.events_kept} kept, "
            f"{self.rejections} rejected extractions"
        )
        if self.events_deferred:
            text += f", {self.events_deferred} deferred (beyond max_days_ahead)"
        if self.duplicates_merged:
            text += f", {self.duplicates_merged} duplicate calendar entries merged"
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
    firm: str = "",
    display_name: str = "",
    source_timezone: str | None = None,
    keywords: tuple[str, ...] | None = None,
    require_stated_offset: bool = False,
) -> RunReport:
    """Run one firm end to end.

    `firm`, `source_timezone` and `keywords` are per-firm; omitted, they fall
    back to `[source]`, which is exactly the single-firm behaviour that existed
    before and keeps every current caller and test correct.
    """
    now = now or datetime.now(UTC)
    report = RunReport(dry_run=dry_run, firm=firm, display_name=display_name)
    source_tz = ZoneInfo(source_timezone or config.source.timezone)
    calendar_tz = ZoneInfo(config.calendar.timezone)
    gate = config.source.keywords if keywords is None else keywords

    posts = source.fetch()
    report.posts_seen = len(posts)
    seen = {post.post_key: post for post in posts}
    extracted: set[str] = set()

    for post in posts:
        post_state = state.posts.get(post.post_key)
        if post_state is not None and not dry_run:
            post_state.last_seen = now.isoformat()
            # Attribute on every sighting, not only on change: an unchanged post
            # skips extraction below, and a state file written before multi-firm
            # support would otherwise never gain a firm at all.
            post_state.firm = firm or post_state.firm
            post_state.url = post.url

        if not _is_relevant(post, gate):
            logger.info("Post %s has no relevant keywords; skipping", post.post_key)
            continue
        report.posts_relevant += 1

        if post_state is not None and post_state.content_hash == post.content_hash:
            if post_state.deferred is not None:
                logger.info("Post %s unchanged; skipping LLM call", post.post_key)
                report.posts_skipped_unchanged += 1
                continue
            # Written before far-future events were kept: whatever this post
            # extracted beyond max_days_ahead back then was dropped, and an
            # unchanged hash would never bring it back. Extract it once more.
            logger.info(
                "Post %s predates deferred-event tracking; re-extracting once", post.post_key
            )

        logger.info("Post %s is new or changed; extracting events", post.post_key)
        raw_events = extractor.extract(post.text)
        events, rejections = validate_events(
            raw_events,
            post,
            config.events,
            source_tz,
            calendar_tz,
            now=now,
            require_stated_offset=require_stated_offset,
        )
        deferred = [r.raw for r in rejections if r.retryable]
        rejected = _count_rejections(post, rejections, report)

        new_post_state = _reconcile(
            post, events, post_state, sink, report, dry_run, now, config.events, state, firm
        )
        new_post_state.firm = firm or (post_state.firm if post_state else "")
        new_post_state.url = post.url
        new_post_state.deferred = [raw.model_dump() for raw in deferred]
        new_post_state.rejected = rejected
        if not dry_run:
            state.posts[post.post_key] = new_post_state
        extracted.add(post.post_key)

    # Far-future events come within range by the calendar alone, with the post
    # unchanged — or already gone from the index page (FTMO only lists recent
    # posts). Re-validate what each of this firm's posts is holding back.
    for key, held in list(state.posts.items()):
        if held.deferred and held.firm == firm and key not in extracted:
            _promote_deferred(
                seen.get(key) or SourcePost(post_key=key, title="", text="", url=held.url),
                held,
                sink,
                report,
                dry_run,
                config.events,
                source_tz,
                calendar_tz,
                now,
                require_stated_offset,
                state,
                firm,
            )

    _detect_anomalies(report, gate)

    if not dry_run:
        _merge_duplicate_windows(state, firm, sink, report, now)
        state.prune(now=now)
    return report


def _count_rejections(
    post: SourcePost, rejections: list[Rejection], report: RunReport
) -> list[str]:
    """Tally rejections; returns (and raises an anomaly for) the ones that matter.

    A rejection used to be a log line and a number in the summary. But one
    that is not benign means the announcement holds an event the calendar
    will not show — E8 rows without a stated offset, a duration over the cap,
    a garbled time — and the only place that said so was a log nobody reads
    while /healthz stayed green.
    """
    flagged: list[str] = []
    for rejection in rejections:
        raw = rejection.raw
        if rejection.retryable:
            report.events_deferred += 1
            logger.info(
                "Deferred %s %s from %s: %s; will re-check each run",
                raw.event_type,
                raw.start_time,
                post.post_key,
                rejection.reason,
            )
            continue
        report.rejections += 1
        logger.warning("Rejected extraction for %s: %s", post.post_key, rejection.reason)
        if not rejection.benign:
            what = f" ({raw.affected})" if raw.affected else ""
            flagged.append(f"{raw.event_type} {raw.start_time}{what}: {rejection.reason}")
    if flagged:
        where = f"{report.label}: " if report.firm else ""
        message = (
            f"{where}post {post.post_key}: {len(flagged)} extracted event(s) rejected and "
            f"not published — {'; '.join(flagged)}"
        )
        logger.error("%s", message)
        report.anomalies.append(message)
        report.rejected_lines.extend(f"{post.post_key}: {line}" for line in flagged)
    return flagged


def _promote_deferred(
    post: SourcePost,
    post_state: PostState,
    sink: EventSink,
    report: RunReport,
    dry_run: bool,
    rules: EventRules,
    source_tz: ZoneInfo,
    calendar_tz: ZoneInfo,
    now: datetime,
    require_stated_offset: bool,
    state: State,
    firm: str,
) -> None:
    """Publish held-back events that are now within max_days_ahead. No LLM call."""
    raws: list[RawEvent] = []
    for item in post_state.deferred or []:
        try:
            raws.append(RawEvent.model_validate(item))
        except ValidationError as e:
            logger.warning("Dropping unreadable deferred event on %s: %s", post.post_key, e)
    events, rejections = validate_events(
        raws,
        post,
        rules,
        source_tz,
        calendar_tz,
        now=now,
        require_stated_offset=require_stated_offset,
    )
    still_deferred = [r.raw for r in rejections if r.retryable]
    # Only what changed state counts: a still-deferred event was already
    # counted on the run that extracted it.
    newly_rejected = _count_rejections(post, [r for r in rejections if not r.retryable], report)
    known = {e.event_key for e in post_state.events}
    promoted: list[TrackedEvent] = []
    for event in events:
        if event.event_key in known:
            continue
        logger.info("Deferred event %s is now within range; publishing", event.event_key)
        tracked = _publish(event, sink, report, dry_run, state, firm, post.post_key)
        if tracked is not None:
            promoted.append(tracked)
    if not dry_run:
        post_state.events.extend(promoted)
        post_state.deferred = [raw.model_dump() for raw in still_deferred]
        post_state.rejected += newly_rejected


def _detect_anomalies(report: RunReport, keywords: tuple[str, ...]) -> None:
    """Flag whole-run outcomes that mean the source moved rather than went quiet.

    Posts exist but none of them matched a keyword: either the firm reworded
    (the gate looks for "maintenance", so an announcement titled "scheduled
    downtime" passes straight through) or the scraper is now reading the wrong
    part of a redesigned page. Both empty the calendar while every step
    reports success.

    With several firms configured, this has to be judged *per firm*: nine
    healthy sources averaged with one that has silently stopped matching still
    look like a working calendar, and the tenth firm's subscribers are the ones
    who get caught by an outage. Each firm's report carries its own anomalies
    and the caller keeps them labelled.
    """
    if report.posts_seen > 0 and report.posts_relevant == 0:
        where = f"{report.label}: " if report.firm else ""
        message = (
            f"{where}keyword gate matched none of {report.posts_seen} scraped post(s) — "
            f"the announcement wording or the page structure may have changed "
            f"(keywords: {', '.join(keywords)})"
        )
        logger.error("%s", message)
        report.anomalies.append(message)


Window = tuple[str, str, str, str, str]


def _window(firm: str, event_type: str, start: str, end: str, summary: str) -> Window:
    """What makes two events the same interruption for a subscriber.

    Summary is included — it carries the affected symbols, so two posts closing
    different instruments at the same minute stay two events. The ICS feed and
    status page dedupe on the same identity.
    """
    return (firm, event_type, start, end, summary)


def _tracked_window(firm: str, tracked: TrackedEvent) -> Window:
    return _window(firm, tracked.event_type, tracked.start, tracked.end, tracked.summary)


def _shared_window(
    state: State, firm: str, post_key: str, event: TradingEvent
) -> TrackedEvent | None:
    """An event another post of this firm already tracks for the same window."""
    wanted = _window(
        firm, event.event_type.value, event.start.isoformat(), event.end.isoformat(), event.summary
    )
    for key, post in state.posts.items():
        if key == post_key or post.firm != firm:
            continue
        for tracked in post.events:
            if tracked.start and _tracked_window(firm, tracked) == wanted:
                return tracked
    return None


def _other_references(state: State, backend_id: str, post_key: str) -> list[str]:
    """Posts other than `post_key` whose events point at this calendar entry.

    The reference count is derived from the state rather than stored, so it
    cannot drift from what the posts actually track.
    """
    return [
        key
        for key, post in state.posts.items()
        if key != post_key and any(e.google_event_id == backend_id for e in post.events)
    ]


def _merge_duplicate_windows(
    state: State, firm: str, sink: EventSink, report: RunReport, now: datetime
) -> None:
    """Collapse duplicate calendar entries created before windows were shared.

    State written by earlier versions has one Google event per announcing
    post for the same window. The oldest post's entry survives (posts are in
    arrival order), the others are deleted from the calendar and their
    TrackedEvents repointed. A failed delete leaves that duplicate as it was,
    to be retried next run. Idempotent: merged windows share one id.
    """
    groups: dict[Window, list[TrackedEvent]] = {}
    for post in state.posts.values():
        if post.firm != firm:
            continue
        for tracked in post.events:
            if tracked.start and _future(tracked, now):
                groups.setdefault(_tracked_window(firm, tracked), []).append(tracked)
    for members in groups.values():
        survivor = members[0].google_event_id
        extras = {m.google_event_id for m in members} - {survivor}
        for extra in sorted(extras):
            try:
                sink.delete_event(extra)
            except Exception as e:  # noqa: BLE001 - retried next run; never fail the sync
                logger.warning("Could not remove duplicate calendar entry %s: %s", extra, e)
                continue
            for member in members:
                if member.google_event_id == extra:
                    member.google_event_id = survivor
            report.duplicates_merged += 1
            logger.info("Merged duplicate calendar entry %s into %s", extra, survivor)


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
        evidence=event.evidence,
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
    state: State,
    firm: str,
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
        where = f"{report.label}: " if report.firm else ""
        message = (
            f"{where}post {post.post_key} previously extracted {len(old)} event(s) and now "
            f"extracts {len(events)} with none new — refusing to delete {len(pending)} "
            "future event(s); "
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
        sharers = _other_references(state, old_event.google_event_id, post.post_key)
        if sharers:
            # Another post still announces this window and shares the one
            # calendar entry: this post letting go is not a withdrawal.
            logger.info(
                "Post %s no longer lists %s; still announced by %s, keeping the entry",
                post.post_key,
                key,
                ", ".join(sharers),
            )
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
        published = _publish(event, sink, report, dry_run, state, firm, post.post_key)
        if published is not None:
            tracked.append(published)

    return PostState(content_hash=post.content_hash, last_seen=now.isoformat(), events=tracked)


def _publish(
    event: TradingEvent,
    sink: EventSink,
    report: RunReport,
    dry_run: bool,
    state: State,
    firm: str,
    post_key: str,
) -> TrackedEvent | None:
    """Create (or adopt) one event in the sink; None on a dry run.

    A window another post of the same firm already tracks is *shared*, not
    created again: FTMO re-announces holiday schedules in follow-up posts, and
    each post used to get its own Google event for the same window. The new
    post's TrackedEvent points at the existing calendar entry instead, and
    the entry is deleted only once no post references it (see _reconcile).
    """
    shared = _shared_window(state, firm, post_key, event)
    if shared is not None:
        logger.info(
            "Event %s is the same window as %s; sharing its calendar entry",
            event.event_key,
            shared.event_key,
        )
        report.events_kept += 1
        return _track(event, shared.google_event_id)
    if dry_run:
        logger.info("[dry-run] would create '%s' at %s", event.summary, event.start)
        report.events_created += 1
        report.created_lines.append(_describe_event(event))
        return None
    existing_id = sink.find_event_id_by_key(event.event_key)
    if existing_id:
        logger.info("Event %s already in calendar; adopting it", event.event_key)
        report.events_kept += 1
        return _track(event, existing_id)
    google_id = sink.create_event(event)
    report.events_created += 1
    report.created_lines.append(_describe_event(event))
    return _track(event, google_id)
