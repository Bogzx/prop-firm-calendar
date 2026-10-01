"""The hand-written feed, read back by an independent RFC 5545 implementation.

test_ics.py checks the bytes the writer means to produce. This checks that
someone else's parser (icalendar, with dateutil interpreting the VTIMEZONE)
reads them back as the same events at the same instants — the thing a
subscriber's calendar app actually depends on.

The decisive check is the time zone: every DTSTART/DTEND is resolved through
the feed's *own* VTIMEZONE (`lookup_tzid=False`), never through the system's
tz database, so a wrong offset or a misplaced transition in the generated
VTIMEZONE shows up as an event at the wrong instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

import icalendar
import pytest

from prop_firm_calendar.sinks.ics import render_ics
from prop_firm_calendar.state import PostState, State, TrackedEvent

NOW = datetime(2026, 1, 10, tzinfo=UTC)

#: UTC instants on both sides of every 2026 transition of the zones below,
#: plus the repeated hour itself when clocks go back (Europe/London 25 Oct
#: 01:00–02:00 local happens twice; 01:30 UTC is the second 01:30).
INSTANTS = [
    datetime(2026, 1, 15, 9, 30, tzinfo=UTC),
    datetime(2026, 3, 8, 7, 30, tzinfo=UTC),  # US spring forward at 08:00 UTC
    datetime(2026, 3, 8, 8, 30, tzinfo=UTC),
    datetime(2026, 3, 29, 0, 30, tzinfo=UTC),  # EU spring forward at 01:00 UTC
    datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
    datetime(2026, 4, 4, 15, 30, tzinfo=UTC),  # Sydney falls back at 16:00 UTC
    datetime(2026, 4, 4, 16, 30, tzinfo=UTC),
    datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    datetime(2026, 10, 3, 15, 30, tzinfo=UTC),  # Sydney springs forward at 16:00 UTC
    datetime(2026, 10, 25, 0, 30, tzinfo=UTC),  # EU falls back at 01:00 UTC
    datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
    datetime(2026, 11, 1, 6, 30, tzinfo=UTC),  # US falls back at 06:00 UTC
    datetime(2026, 11, 1, 7, 30, tzinfo=UTC),
    datetime(2026, 12, 24, 18, 0, tzinfo=UTC),
]

ZONES = ["America/Chicago", "Europe/London", "Australia/Sydney", "Etc/GMT-3", "UTC"]

#: What the escaping has to survive: emoji (multi-byte, so folding must not
#: split them), and the characters RFC 5545 escapes — including a literal
#: backslash-n, which a writer that forgot to escape "\\" turns into a newline.
SUMMARY = "⚠️ Platform Maintenance — MT4, MT5; cTrader \\n \\, all"
EVIDENCE = 'Saturday, 6 June; "08:00 – 14:00" (GMT+3)\nall platforms'
LONG = "⏳ Early Close — " + ", ".join(f"SYMBOL{i}.cash" for i in range(12))


def _state() -> State:
    events = []
    for i, start in enumerate(INSTANTS):
        events.append(
            TrackedEvent(
                event_key=f"{i:016x}",
                google_event_id=f"ics:{i}",
                start=start.isoformat(),
                # An hour long: some events start before a transition and end after it.
                end=(start + timedelta(hours=1)).isoformat(),
                summary=LONG if i % 3 == 0 else f"{SUMMARY} {i}",
                event_type="maintenance",
                evidence=EVIDENCE if i % 2 else "",
            )
        )
    post = PostState(
        content_hash="h",
        last_seen=NOW.isoformat(),
        events=events,
        firm="ftmo",
        url="https://example.com/announcement?a=1;b=2",
    )
    return State(posts={"post": post})


def _render(tz_name: str, now: datetime = NOW) -> str:
    return render_ics(_state(), (60, 10), refresh_minutes=360, tz_name=tz_name, now=now)


def _resolve(value: datetime, feed_tz: tzinfo | None) -> datetime:
    """A parsed DTSTART as an instant, using only the feed's own VTIMEZONE."""
    if value.tzinfo is not None and feed_tz is None:
        return value  # a UTC ("Z") timestamp
    assert feed_tz is not None, "a TZID-local time but no VTIMEZONE to read it with"
    return value.replace(tzinfo=None).replace(tzinfo=feed_tz)


@pytest.fixture(params=ZONES)
def parsed(request: pytest.FixtureRequest) -> tuple[str, str, icalendar.Calendar]:
    feed = _render(request.param)
    return request.param, feed, icalendar.Calendar.from_ical(feed)


def test_every_event_parses_with_its_identity(parsed) -> None:
    _, _, cal = parsed
    events = cal.walk("VEVENT")
    assert len(events) == len(INSTANTS)
    uids = [str(e["UID"]) for e in events]
    assert len(set(uids)) == len(uids)
    for event in events:
        for prop in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "TRANSP"):
            assert prop in event, prop
        assert str(event["TRANSP"]) == "TRANSPARENT"
    assert str(cal["VERSION"]) == "2.0"
    assert "PRODID" in cal


def test_every_instant_survives_the_feeds_own_vtimezone(parsed) -> None:
    tz_name, _, cal = parsed
    zones = cal.walk("VTIMEZONE")
    assert len(zones) == (0 if tz_name == "UTC" else 1)
    feed_tz = zones[0].to_tz(lookup_tzid=False) if zones else None
    got = [
        (
            _resolve(e["DTSTART"].dt, feed_tz if "TZID" in e["DTSTART"].params else None),
            _resolve(e["DTEND"].dt, feed_tz if "TZID" in e["DTEND"].params else None),
        )
        for e in cal.walk("VEVENT")
    ]
    want = [(start, start + timedelta(hours=1)) for start in INSTANTS]
    assert [(s.astimezone(UTC), e.astimezone(UTC)) for s, e in got] == want


def test_text_round_trips_through_escaping(parsed) -> None:
    _, _, cal = parsed
    events = cal.walk("VEVENT")
    for i, event in enumerate(events):
        assert str(event["SUMMARY"]) == (LONG if i % 3 == 0 else f"{SUMMARY} {i}")
        description = str(event["DESCRIPTION"])
        assert "Source: https://example.com/announcement?a=1;b=2" in description
        if i % 2:
            # Line breaks inside the quote are flattened by the writer's own
            # evidence cleaning before they get here; the rest is verbatim.
            assert description.startswith('“Saturday, 6 June; "08:00 – 14:00" (GMT+3)')


def test_every_alarm_is_complete(parsed) -> None:
    _, _, cal = parsed
    for event in cal.walk("VEVENT"):
        alarms = event.walk("VALARM")
        assert [str(a["TRIGGER"].to_ical(), "ascii") for a in alarms] == ["-PT1H", "-PT10M"]
        for alarm in alarms:
            assert str(alarm["ACTION"]) == "DISPLAY"
            assert str(alarm["DESCRIPTION"]) == str(event["SUMMARY"])


def test_lines_are_crlf_folded_at_75_octets_on_character_boundaries(parsed) -> None:
    _, feed, _ = parsed
    raw = feed.encode("utf-8")
    assert raw.endswith(b"\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b""), "bare LF"
    assert b"\r" not in raw.replace(b"\r\n", b""), "bare CR"
    lines = raw.split(b"\r\n")[:-1]
    assert max(len(line) for line in lines) <= 75
    continuations = [line for line in lines if line.startswith(b" ")]
    assert continuations, "the fixture is meant to exercise folding"
    for line in continuations:
        assert line[1] & 0xC0 != 0x80, f"fold splits a UTF-8 character: {line!r}"


def test_uids_are_stable_across_syncs() -> None:
    """Apps match on UID: a re-render must not move it, only DTSTAMP."""
    first = icalendar.Calendar.from_ical(_render("Europe/London"))
    later = icalendar.Calendar.from_ical(_render("Europe/London", NOW + timedelta(hours=6)))
    assert [str(e["UID"]) for e in first.walk("VEVENT")] == [
        str(e["UID"]) for e in later.walk("VEVENT")
    ]
    assert first.walk("VEVENT")[0]["DTSTAMP"].dt != later.walk("VEVENT")[0]["DTSTAMP"].dt
