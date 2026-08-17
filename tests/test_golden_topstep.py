"""Golden test: Topstep's real holiday table, pinned end-to-end to its events.

Same discipline as test_golden_extraction.py does for FTMO — recorded HTML in,
calendar events out, with the model's half pinned as reviewable data rather
than called live. What it adds is the reason Topstep is worth shipping at all:
its announcement spans a whole year in a daylight-saving zone, so it is the one
source where getting the timezone wrong is *visible* rather than seasonal. Five
of the thirteen rows fall in CDT and eight in CST; a fixed offset passes eight
of them and quietly breaks five.
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

FIXTURES = Path(__file__).parent / "fixtures" / "topstep"
PROFILE = load_profile("topstep")
CT = ZoneInfo(PROFILE.timezone)
# The feed's display zone is global and unchanged by multi-firm support; every
# firm's events are stored converted into it. Correctness is the instant, not
# the wall clock the feed happens to print.
CALENDAR_TZ = ZoneInfo(FTMO_PLATFORM_TZ)

# Just before the first row of the table, so the whole year is still ahead.
NOW = datetime(2025, 12, 31, 12, 0, tzinfo=UTC)
# The real table runs 12+ months out; the shipped 120-day cap would reject most
# of it. Widened here so the golden checks every row rather than the near ones.
RULES = EventRules(max_days_ahead=400)

POST = WebSource(PROFILE).parse_listing((FIXTURES / "listing.html").read_text(encoding="utf-8"))[0]
GOLDEN = json.loads((FIXTURES / "holiday-trading-hours.expected.json").read_text(encoding="utf-8"))
RAW_EVENTS = [RawEvent(**e) for e in GOLDEN["events"]]

# (start CT wall clock, end CT wall clock, UTC offset that date really has)
EXPECTED_CT = [
    ("2026-01-01 00:00", "2026-01-01 23:59", "-0600"),
    ("2026-01-19 11:45", "2026-01-19 23:59", "-0600"),
    ("2026-02-16 11:45", "2026-02-16 23:59", "-0600"),
    ("2026-04-03 08:00", "2026-04-03 23:59", "-0500"),
    ("2026-05-25 11:45", "2026-05-25 23:59", "-0500"),
    ("2026-06-19 11:10", "2026-06-19 23:59", "-0500"),
    ("2026-07-03 11:45", "2026-07-03 23:59", "-0500"),
    ("2026-09-07 11:45", "2026-09-07 23:59", "-0500"),
    ("2026-11-26 11:45", "2026-11-26 23:59", "-0600"),
    ("2026-11-27 12:00", "2026-11-27 23:59", "-0600"),
    ("2026-12-24 12:00", "2026-12-24 23:59", "-0600"),
    ("2026-12-25 00:00", "2026-12-25 23:59", "-0600"),
    ("2027-01-01 00:00", "2027-01-01 23:59", "-0600"),
]


def _validate(rules: EventRules | None = None):
    return validate_events(RAW_EVENTS, POST, rules or RULES, CT, CALENDAR_TZ, now=NOW)


# -- the scrape -----------------------------------------------------------


def test_scraped_text_still_contains_every_announced_row() -> None:
    """If Intercom's markup moves, this fails before anything reaches a model."""
    text = POST.text
    assert "2026 Holiday Schedule" in text
    for holiday in (
        "New Year's Day",
        "MLK Day",
        "Presidents' Day",
        "Good Friday",
        "Memorial Day",
        "Juneteenth",
        "Independence Day",
        "Labor Day",
        "Thanksgiving",
        "Christmas Eve",
        "Christmas Day",
    ):
        assert holiday in text, f"{holiday} missing from the scraped announcement"
    assert "11:45 CT" in text and "Markets closed" in text


def test_related_articles_teasers_are_stripped_from_the_body() -> None:
    """Intercom nests a related-posts strip inside <article>.

    Those headlines are confident, unrelated prose; left in, they are exactly
    the material an extractor turns into plausible, wrong calendar entries.
    """
    assert "Related Articles" not in POST.text
    assert "Topstep Payout Policy" not in POST.text


def test_post_identity_is_stable_across_rewrites() -> None:
    """The article is edited in place as the year advances; its key must not move."""
    assert POST.post_key == "13350348-topstep-holiday-trading-hours"


def test_announcement_passes_the_profile_keyword_gate() -> None:
    assert _is_relevant(POST, PROFILE.keywords)


# -- extraction -> events -------------------------------------------------


def test_thirteen_events_zero_rejections() -> None:
    events, rejections = _validate()
    assert rejections == []
    assert len(events) == 13


def test_every_event_matches_its_announced_central_time() -> None:
    """The whole point: each row read back in CT is what Topstep printed."""
    events, _ = _validate()
    actual = [
        (
            e.start.astimezone(CT).strftime("%Y-%m-%d %H:%M"),
            e.end.astimezone(CT).strftime("%Y-%m-%d %H:%M"),
            e.start.astimezone(CT).strftime("%z"),
        )
        for e in events
    ]
    assert actual == EXPECTED_CT


def test_daylight_saving_is_applied_per_date_not_pinned() -> None:
    """A fixed offset would pass eight rows and silently break five.

    Topstep's table straddles both US changeovers (2026-03-08 and 2026-11-01),
    so the correct answer is two different UTC offsets within one announcement.
    """
    events, _ = _validate()
    offsets = {e.start.astimezone(CT).strftime("%z") for e in events}
    assert offsets == {"-0500", "-0600"}, "CT must resolve per date, not as one offset"

    # Read the offset back *in CT*: events are stored converted into the feed's
    # display zone, so e.start.utcoffset() is that zone's offset, not Topstep's.
    summer = next(e for e in events if e.start.astimezone(CT).month == 7)
    winter = next(e for e in events if e.start.astimezone(CT).month == 1)
    assert summer.start.astimezone(CT).utcoffset().total_seconds() == -5 * 3600
    assert winter.start.astimezone(CT).utcoffset().total_seconds() == -6 * 3600


def test_utc_instants_are_correct() -> None:
    """Independent check on the instants, not just the wall clock we printed."""
    events, _ = _validate()
    by_start = {e.start.astimezone(CT).strftime("%Y-%m-%d"): e for e in events}
    # 11:45 CDT on 3 July 2026 == 16:45 UTC
    assert by_start["2026-07-03"].start.astimezone(UTC).strftime("%H:%M") == "16:45"
    # 11:45 CST on 26 November 2026 == 17:45 UTC
    assert by_start["2026-11-26"].start.astimezone(UTC).strftime("%H:%M") == "17:45"


def test_event_type_distribution() -> None:
    """Three whole-day closures; ten early closes. Reopens produce nothing."""
    events, _ = _validate()
    counts: dict[EventType, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    assert counts == {EventType.HOLIDAY_CLOSURE: 3, EventType.EARLY_CLOSE: 10}


def test_event_keys_are_unique_and_stable() -> None:
    first, _ = _validate()
    second, _ = _validate()
    keys = [e.event_key for e in first]
    assert len(set(keys)) == 13
    assert keys == [e.event_key for e in second]


def test_stale_rows_do_not_leak_into_the_feed() -> None:
    """The table keeps last winter's rows; a run today must not publish them."""
    events, rejections = validate_events(
        RAW_EVENTS,
        POST,
        EventRules(max_days_ahead=400),
        CT,
        CALENDAR_TZ,
        now=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
    )
    starts = {e.start.astimezone(CT).strftime("%Y-%m-%d") for e in events}
    assert starts == {
        "2026-09-07",
        "2026-11-26",
        "2026-11-27",
        "2026-12-24",
        "2026-12-25",
        "2027-01-01",
    }
    assert all(r.reason == "already ended" for r in rejections)
