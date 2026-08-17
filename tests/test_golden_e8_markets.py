"""Golden test: E8 Markets' real monthly schedule, and its refusal to guess an hour.

E8 is the firm that has no correct timezone. Their own help centre says the
server moves to UTC+2 "at the beginning of November" and UTC+3 "at the end of
March" — a window that matches no IANA zone, since Europe/Athens reverts a week
earlier and a fixed Etc/GMT-3 never moves at all. Whatever zone were chosen for
them would be an hour wrong for about a week each year.

What makes them shippable anyway is that every row of their schedule carries
its own "(gmt+3)". So the profile sets require_stated_offset: the announcement's
own offset is used, and a row that omits one is rejected instead of published.
The last test here is the one that matters — it is the proof that the fallback
really is off.
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

FIXTURES = Path(__file__).parent / "fixtures" / "e8-markets"
PROFILE = load_profile("e8-markets")
CALENDAR_TZ = ZoneInfo(FTMO_PLATFORM_TZ)
# Deliberately a zone that is WRONG for E8, so that any event which slips
# through the require_stated_offset gate lands somewhere obviously incorrect
# rather than accidentally right.
WRONG_FALLBACK = ZoneInfo("America/New_York")

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

POST = WebSource(PROFILE).parse_listing((FIXTURES / "listing.html").read_text(encoding="utf-8"))[0]
GOLDEN = json.loads(
    (FIXTURES / "july-holiday-trading-schedule.expected.json").read_text(encoding="utf-8")
)
RAW_EVENTS = [RawEvent(**e) for e in GOLDEN["events"]]


def _validate(raw: list[RawEvent] | None = None, now: datetime | None = None):
    return validate_events(
        raw if raw is not None else RAW_EVENTS,
        POST,
        EventRules(),
        WRONG_FALLBACK,
        CALENDAR_TZ,
        now=now or NOW,
        require_stated_offset=PROFILE.require_stated_offset,
    )


# -- the scrape -----------------------------------------------------------


def test_scraped_text_contains_the_whole_table() -> None:
    text = POST.text
    assert "Trading schedule (Forex and Crypto accounts)" in text
    assert "Friday - July 3rd" in text
    for symbol in ("WTI", "BRENT", "XAUUSD", "XAGUSD", "DOW", "NIKKEI", "NSDQ", "SP"):
        assert symbol in text
    assert "(gmt+3)" in text, "the per-row offset is what makes this source publishable"
    assert "7/03/2026" in text, "the year is only recoverable from the futures line"


def test_post_identity_is_the_stable_numeric_id() -> None:
    """The slug rotates monthly; the id does not, so neither does the post key."""
    assert POST.post_key == "12122593"


def test_announcement_passes_the_profile_keyword_gate() -> None:
    assert _is_relevant(POST, PROFILE.keywords)


# -- extraction -> events -------------------------------------------------


def test_two_grouped_events_zero_rejections() -> None:
    """Seven instruments at 20:00 and BRENT at 20:30 — two events, not eight."""
    events, rejections = _validate()
    assert rejections == []
    assert len(events) == 2
    assert all(e.event_type is EventType.EARLY_CLOSE for e in events)


def test_events_use_the_offset_the_article_printed() -> None:
    events, _ = _validate()
    stated = ZoneInfo("Etc/GMT-3")  # what "(gmt+3)" means, for readback only
    actual = [
        (
            e.start.astimezone(stated).strftime("%Y-%m-%d %H:%M"),
            e.end.astimezone(stated).strftime("%H:%M"),
            e.summary,
        )
        for e in events
    ]
    seven = "⏳ Early Close — WTI, XAUUSD, XAGUSD, DOW, NIKKEI, NSDQ, SP"
    assert actual == [
        ("2026-07-03 20:00", "23:59", seven),
        ("2026-07-03 20:30", "23:59", "⏳ Early Close — BRENT"),
    ]


def test_utc_instants_match_the_stated_gmt3() -> None:
    """20:00 (gmt+3) is 17:00 UTC — and nothing near a New York reading of it."""
    events, _ = _validate()
    assert events[0].start.astimezone(UTC).strftime("%H:%M") == "17:00"
    assert events[1].start.astimezone(UTC).strftime("%H:%M") == "17:30"


def test_event_keys_are_unique_and_stable() -> None:
    first, _ = _validate()
    second, _ = _validate()
    keys = [e.event_key for e in first]
    assert len(set(keys)) == 2
    assert keys == [e.event_key for e in second]


# -- the refusal to guess -------------------------------------------------


def test_a_row_without_a_stated_offset_is_rejected_not_guessed() -> None:
    """The whole reason E8 is safe to ship.

    Their platform clock follows no IANA zone, so an event whose announcement
    did not say the offset has no defensible hour. It must be dropped, loudly,
    rather than published against the profile's nominal timezone.
    """
    offsetless = [e.model_copy(update={"stated_utc_offset": None}) for e in RAW_EVENTS]
    events, rejections = _validate(offsetless)
    assert events == []
    assert len(rejections) == 2
    assert all("refusing to guess the hour" in r.reason for r in rejections)


def test_other_firms_still_fall_back_to_their_timezone() -> None:
    """The refusal is opt-in per profile and must not leak to everyone else."""
    assert load_profile("ftmo").require_stated_offset is False
    assert load_profile("topstep").require_stated_offset is False
    assert load_profile("blueberry-funded").require_stated_offset is False

    offsetless = [e.model_copy(update={"stated_utc_offset": None}) for e in RAW_EVENTS]
    events, rejections = validate_events(
        offsetless, POST, EventRules(), ZoneInfo("Etc/GMT-3"), CALENDAR_TZ, now=NOW
    )
    assert rejections == []
    assert len(events) == 2
