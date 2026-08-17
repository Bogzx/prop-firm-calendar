"""ICS (RFC 5545) feed generation.

The feed is a projection of the state file — regenerated wholesale after each
run. Anyone can subscribe to the resulting file/URL from Google, Apple, or
Outlook calendars without OAuth.

Event times are written as local times in the calendar's timezone with a TZID
reference and a matching VTIMEZONE block — the same representation calendar
apps use in their own exports. Clients still convert to each viewer's
timezone, but the raw feed reads in the calendar's timezone (matching FTMO's
announced times) instead of UTC, which read "shifted" to anyone east of
Greenwich and broke on naive parsers that drop the Z suffix.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ftmo_calendar.state import State, TrackedEvent

logger = logging.getLogger(__name__)

_PRODID = "-//AutoFtmoCalendar//ftmo-calendar//EN"


def _escape(text: str) -> str:
    # \r is stripped (not escaped): raw CR/LF in a property value would let
    # crafted upstream text inject arbitrary ICS lines into subscriber feeds.
    text = text.replace("\r", "")
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_offset(offset: timedelta) -> str:
    total = round(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, remainder = divmod(abs(total), 3600)
    return f"{sign}{hours:02d}{remainder // 60:02d}"


def _transitions(
    tz: ZoneInfo, start: datetime, end: datetime
) -> Iterator[tuple[datetime, timedelta, timedelta]]:
    """Yield (utc_onset, offset_before, offset_after) for each UTC-offset change.

    zoneinfo exposes no transition table, so changes are found by probing the
    UTC timeline day by day and bisecting each change down to the minute
    (real-world transitions fall on whole minutes).
    """
    step = timedelta(days=1)
    t = start
    prev_offset = t.astimezone(tz).utcoffset() or timedelta(0)
    while t < end:
        nxt = min(t + step, end)
        offset = nxt.astimezone(tz).utcoffset() or timedelta(0)
        if offset != prev_offset:
            lo, hi = t, nxt
            while hi - lo > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if (mid.astimezone(tz).utcoffset() or timedelta(0)) == prev_offset:
                    lo = mid
                else:
                    hi = mid
            yield hi.replace(second=0, microsecond=0), prev_offset, offset
        prev_offset = offset
        t = nxt


def _observance(
    kind: str, local_onset: datetime, offset_from: str, offset_to: str, name: str | None
) -> list[str]:
    lines = [
        f"BEGIN:{kind}",
        f"DTSTART:{local_onset.strftime('%Y%m%dT%H%M%S')}",
        f"TZOFFSETFROM:{offset_from}",
        f"TZOFFSETTO:{offset_to}",
    ]
    if name:
        lines.append(f"TZNAME:{_escape(name)}")
    lines.append(f"END:{kind}")
    return lines


def _vtimezone_lines(tz: ZoneInfo, first: datetime, last: datetime) -> list[str]:
    """VTIMEZONE block covering [first, last], or [] for plain UTC.

    The probe window starts a year before the first event so the observance
    already in effect at that point is always included; any DST zone
    transitions at least once per 366 days. Callers that get [] back (the
    zone is UTC throughout) emit Z timestamps with no TZID instead.
    """
    probe_start = first.astimezone(UTC) - timedelta(days=366)
    probe_end = last.astimezone(UTC) + timedelta(days=1)
    transitions = list(_transitions(tz, probe_start, probe_end))
    base = probe_start.astimezone(tz)
    base_offset = base.utcoffset() or timedelta(0)
    if not transitions and not base_offset:
        return []

    lines = ["BEGIN:VTIMEZONE", f"TZID:{tz.key}"]
    if not transitions:
        # Fixed-offset zone: one observance, conventionally anchored at epoch.
        offset = _ics_offset(base_offset)
        lines += _observance("STANDARD", datetime(1970, 1, 1), offset, offset, base.tzname())  # noqa: DTZ001 - deliberate naive local time
    for utc_onset, before, after in transitions:
        local_after = utc_onset.astimezone(tz)
        kind = "DAYLIGHT" if local_after.dst() else "STANDARD"
        # DTSTART of an observance is the onset wall-clock time in the OLD offset.
        local_onset = (utc_onset + before).replace(tzinfo=None)
        lines += _observance(
            kind, local_onset, _ics_offset(before), _ics_offset(after), local_after.tzname()
        )
    lines.append("END:VTIMEZONE")
    return lines


def calendar_name(firm_names: list[str], firm_titles: Mapping[str, str] | None = None) -> str:
    """Name the calendar after what is actually in it.

    A feed carrying one firm keeps that firm's name — which is what makes the
    unfiltered feed of an FTMO-only deployment come out byte-identical to the
    one people are already subscribed to. A feed carrying several must not go
    on calling itself after one of them: a subscriber whose app shows "FTMO
    Trading Updates" while the feed also contains Topstep closures has been
    silently misinformed, which is worse than an unfamiliar name.
    """
    titles = firm_titles or {}
    if len(firm_names) == 1:
        return f"{titles.get(firm_names[0], firm_names[0].upper())} Trading Updates"
    if len(firm_names) > 1:
        return "Prop Firm Trading Updates"
    return "FTMO Trading Updates"  # empty feed: keep the historical name


def render_ics(
    state: State,
    reminders_minutes: tuple[int, ...],
    *,
    source_url: str = "",
    refresh_minutes: int = 0,
    types: frozenset[str] | None = None,
    firms: frozenset[str] | None = None,
    default_firm: str = "",
    firm_titles: Mapping[str, str] | None = None,
    tz_name: str = "UTC",
    now: datetime | None = None,
) -> str:
    """Render the feed.

    `types` (EventType values) and `firms` (source-profile names) each limit
    what is included; `None` means "no filter on this axis", which is what the
    long-standing unfiltered feed passes and why its output is unchanged.

    `default_firm` attributes state entries written before per-firm tracking —
    see State.firm_of. Without it, upgrading would drop every existing event
    out of every per-firm feed until it happened to be re-scraped.
    """
    now = now or datetime.now(UTC)
    tz = ZoneInfo(tz_name)
    dtstamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    selected: list[tuple[TrackedEvent, datetime, datetime]] = []
    present: list[str] = []
    for post in state.posts.values():
        firm = state.firm_of(post, default_firm)
        if firms is not None and firm not in firms:
            continue
        for event in post.events:
            if not event.summary or not event.start:
                continue  # pre-v2 state entry without display data
            if types is not None and event.event_type not in types:
                continue
            if firm and firm not in present:
                present.append(firm)
            selected.append(
                (event, datetime.fromisoformat(event.start), datetime.fromisoformat(event.end))
            )

    vtimezone: list[str] = []
    if selected:
        vtimezone = _vtimezone_lines(
            tz, min(s for _, s, _ in selected), max(e for _, _, e in selected)
        )

    # Name after what was ASKED for when a firm filter is present, not after
    # what happened to match: `?firms=e8-markets` returning nothing this week is
    # still an E8 Markets feed, and must not inherit another firm's name. With
    # no filter, name after what is in the feed, falling back to the configured
    # firms so an empty feed is still labelled correctly.
    named = sorted(firms) if firms is not None else (sorted(present) or sorted(firm_titles or {}))
    name = calendar_name(named, firm_titles)
    if types is not None:
        name += f" ({', '.join(sorted(types))})"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(name)}",
    ]
    if vtimezone:
        lines.append(f"X-WR-TIMEZONE:{tz.key}")
    if refresh_minutes > 0:
        # Hint calendar apps how often to re-poll the subscribed feed.
        lines += [
            f"REFRESH-INTERVAL;VALUE=DURATION:PT{refresh_minutes}M",
            f"X-PUBLISHED-TTL:PT{refresh_minutes}M",
        ]
    lines += vtimezone

    def stamp(prop: str, dt: datetime) -> str:
        if vtimezone:
            return f"{prop};TZID={tz.key}:{dt.astimezone(tz).strftime('%Y%m%dT%H%M%S')}"
        return f"{prop}:{dt.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    for event, start_dt, end_dt in selected:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event.event_key}@ftmo-calendar",
            f"DTSTAMP:{dtstamp}",
            stamp("DTSTART", start_dt),
            stamp("DTEND", end_dt),
            f"SUMMARY:{_escape(event.summary)}",
        ]
        if source_url:
            lines.append(f"DESCRIPTION:Source: {_escape(source_url)}\\nCreated by AutoFtmoCalendar")
        for minutes in reminders_minutes:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape(event.summary)}",
                f"TRIGGER:-PT{minutes}M",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def write_ics(
    state: State,
    path: Path,
    reminders_minutes: tuple[int, ...],
    *,
    source_url: str = "",
    refresh_minutes: int = 0,
    default_firm: str = "",
    firm_titles: Mapping[str, str] | None = None,
    tz_name: str = "UTC",
    now: datetime | None = None,
) -> None:
    content = render_ics(
        state,
        reminders_minutes,
        source_url=source_url,
        refresh_minutes=refresh_minutes,
        default_firm=default_firm,
        firm_titles=firm_titles,
        tz_name=tz_name,
        now=now,
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8", newline="")
    tmp.replace(path)
    logger.info("ICS feed written to %s", path)
