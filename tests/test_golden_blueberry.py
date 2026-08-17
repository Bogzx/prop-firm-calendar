"""Golden test: Blueberry Funded's real crypto-maintenance schedule.

Small announcement, but it pins two things worth pinning. First, the timezone
is a named daylight-saving zone stated as "BST", and the article helpfully
prints the same window in EDT as well — so the feed's instants can be checked
against the firm's own second opinion rather than only against our reading of
the first. Second, the article ends "..and every other Saturday thereafter",
which is precisely the kind of sentence an extractor is tempted to expand into
a year of invented windows; the golden asserts it does not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ftmo_calendar.config import FTMO_PLATFORM_TZ, EventRules
from ftmo_calendar.models import EventType
from ftmo_calendar.parsing.llm import RawEvent
from ftmo_calendar.parsing.validate import validate_events
from ftmo_calendar.pipeline import _is_relevant
from ftmo_calendar.sources.profile import load_profile
from ftmo_calendar.sources.web import WebSource

FIXTURES = Path(__file__).parent / "fixtures" / "blueberry-funded"
PROFILE = load_profile("blueberry-funded")
LONDON = ZoneInfo(PROFILE.timezone)
NEW_YORK = ZoneInfo("America/New_York")
CALENDAR_TZ = ZoneInfo(FTMO_PLATFORM_TZ)

# Before the first listed window.
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

POST = WebSource(PROFILE).parse_listing((FIXTURES / "listing.html").read_text(encoding="utf-8"))[0]
GOLDEN = json.loads((FIXTURES / "crypto-maintenance.expected.json").read_text(encoding="utf-8"))
RAW_EVENTS = [RawEvent(**e) for e in GOLDEN["events"]]

EXPECTED_DATES = ["2026-07-18", "2026-08-01", "2026-08-15", "2026-08-29"]


def _validate(now: datetime | None = None):
    return validate_events(RAW_EVENTS, POST, EventRules(), LONDON, CALENDAR_TZ, now=now or NOW)


# -- the scrape -----------------------------------------------------------


def test_scraped_text_contains_the_whole_schedule() -> None:
    text = POST.text
    assert "Crypto scheduled maintenance runs every second Saturday" in text
    for date_text in ("18 July 2026", "1 August 2026", "15 August 2026", "29 August 2026"):
        assert date_text in text
    assert "BST" in text and "EDT" in text


def test_related_articles_teasers_are_stripped_from_the_body() -> None:
    assert "Related Articles" not in POST.text


def test_post_identity_is_stable() -> None:
    assert POST.post_key == "15967669-when-is-crypto-trading-paused-for-scheduled-maintenance"


def test_announcement_passes_the_profile_keyword_gate() -> None:
    assert _is_relevant(POST, PROFILE.keywords)


# -- extraction -> events -------------------------------------------------


def test_four_windows_zero_rejections() -> None:
    events, rejections = _validate()
    assert rejections == []
    assert len(events) == 4
    assert all(e.event_type is EventType.CRYPTO_CLOSURE for e in events)


def test_every_window_matches_the_announced_bst_wall_clock() -> None:
    events, _ = _validate()
    actual = [
        (
            e.start.astimezone(LONDON).strftime("%Y-%m-%d %H:%M"),
            e.end.astimezone(LONDON).strftime("%H:%M"),
        )
        for e in events
    ]
    assert actual == [(f"{d} 09:00", "10:00") for d in EXPECTED_DATES]


def test_windows_also_match_the_articles_own_edt_column() -> None:
    """The firm printed the same window twice; both readings must agree.

    09:00 BST and 04:00 EDT are the same instant only if BST is read as
    Europe/London rather than as a fixed +01:00 applied blindly. This is the
    firm checking our arithmetic for us.
    """
    events, _ = _validate()
    for event in events:
        assert event.start.astimezone(NEW_YORK).strftime("%H:%M") == "04:00"
        assert event.end.astimezone(NEW_YORK).strftime("%H:%M") == "05:00"
        assert event.start.astimezone(UTC).strftime("%H:%M") == "08:00"


def test_every_window_is_one_hour_on_a_saturday() -> None:
    events, _ = _validate()
    for event in events:
        assert (event.end - event.start).total_seconds() == 3600
        assert event.start.astimezone(LONDON).weekday() == 5  # Saturday


def test_the_recurrence_sentence_does_not_become_invented_events() -> None:
    """ "..and every other Saturday thereafter" schedules nothing by itself."""
    assert "every other Saturday thereafter" in POST.text
    events, _ = _validate()
    dates = [e.start.astimezone(LONDON).strftime("%Y-%m-%d") for e in events]
    assert dates == EXPECTED_DATES, "only the dates actually tabulated may be published"


def test_event_keys_are_unique_and_stable() -> None:
    first, _ = _validate()
    second, _ = _validate()
    keys = [e.event_key for e in first]
    assert len(set(keys)) == 4
    assert keys == [e.event_key for e in second]


def test_past_windows_are_dropped_once_they_have_happened() -> None:
    events, rejections = _validate(now=datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    dates = [e.start.astimezone(LONDON).strftime("%Y-%m-%d") for e in events]
    assert dates == ["2026-08-29"]
    assert len(rejections) == 3
