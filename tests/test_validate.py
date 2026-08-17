from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ftmo_calendar.config import EventRules
from ftmo_calendar.models import EventType, SourcePost
from ftmo_calendar.parsing.llm import RawEvent
from ftmo_calendar.parsing.validate import validate_events

TZ = ZoneInfo("Europe/Bucharest")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
POST = SourcePost(
    post_key="trading-update-2026-06-04",
    title="Trading Update | Jun 4 2026",
    text="Maintenance on Saturday. " * 100,
    url="https://ftmo.com/en/trading-updates/",
)


def raw(start="2026-06-06T08:00:00", end="2026-06-06T14:00:00", **kw) -> RawEvent:
    defaults = dict(
        event_type="maintenance",
        start_time=start,
        end_time=end,
        stated_utc_offset="+03:00",
        confidence="high",
    )
    defaults.update(kw)
    return RawEvent(**defaults)


def run(events, rules=None):
    return validate_events(events, POST, rules or EventRules(), TZ, TZ, now=NOW)


def test_valid_event_converted() -> None:
    events, rejections = run([raw()])
    assert rejections == []
    [event] = events
    assert event.event_type is EventType.MAINTENANCE
    assert event.start.isoformat() == "2026-06-06T08:00:00+03:00"
    assert event.summary == EventRules().summaries["maintenance"]
    assert "https://ftmo.com/en/trading-updates/" in event.description
    assert len(event.description) < 1000  # excerpt is trimmed
    assert event.source_post_key == POST.post_key


def test_missing_offset_uses_source_timezone() -> None:
    events, _ = run([raw(stated_utc_offset=None)])
    assert events[0].start.utcoffset() == datetime(2026, 6, 6, tzinfo=TZ).utcoffset()


def test_end_before_start_rejected() -> None:
    events, rejections = run([raw(start="2026-06-06T14:00:00", end="2026-06-06T08:00:00")])
    assert events == [] and "after start" in rejections[0].reason


def test_overlong_duration_rejected() -> None:
    events, rejections = run([raw(end="2026-06-09T08:00:00")])
    assert events == [] and "duration" in rejections[0].reason


def test_too_far_ahead_rejected() -> None:
    events, rejections = run([raw(start="2027-06-06T08:00:00", end="2027-06-06T14:00:00")])
    assert events == [] and "future" in rejections[0].reason


def test_already_ended_rejected() -> None:
    events, rejections = run([raw(start="2026-05-01T08:00:00", end="2026-05-01T14:00:00")])
    assert events == [] and "ended" in rejections[0].reason


def test_unparseable_datetime_rejected() -> None:
    events, rejections = run([raw(start="whenever")])
    assert events == [] and "datetime" in rejections[0].reason


def test_affected_control_characters_stripped() -> None:
    events, _ = run([raw(affected="US30\r\nX-INJECTED:1\x00")])
    assert "\r" not in events[0].summary and "\n" not in events[0].summary
    assert "US30X-INJECTED:1" in events[0].summary


def test_affected_symbols_in_summary() -> None:
    events, _ = run([raw(event_type="early_close", affected="US30.cash, US100.cash")])
    assert events[0].summary == "⏳ Early Close — US30.cash, US100.cash"


def test_long_affected_list_truncated() -> None:
    events, _ = run([raw(affected=", ".join(f"SYM{i}.cash" for i in range(20)))])
    assert len(events[0].summary) < 110
    assert events[0].summary.endswith("…")


def test_granular_types_validate() -> None:
    for event_type in ("holiday_closure", "early_close", "late_open", "symbol_event"):
        events, rejections = run([raw(event_type=event_type)])
        assert rejections == [] and events[0].event_type.value == event_type


# -- confidence -----------------------------------------------------------
# The model is asked for it, consensus votes on it, and until now nothing read
# it: a guess reached subscribers looking exactly as certain as a stated
# maintenance window.


def test_high_confidence_event_is_unmarked() -> None:
    events, _ = run([raw()])
    assert events[0].confidence == "high"
    assert "unconfirmed" not in events[0].summary
    assert "confidence" not in events[0].description.lower()


def test_low_confidence_event_is_flagged_not_hidden() -> None:
    events, _ = run([raw(confidence="low")])
    [event] = events
    assert event.confidence == "low"
    assert event.summary.endswith("(unconfirmed)")
    assert "LOW" in event.description
    assert "https://ftmo.com/en/trading-updates/" in event.description  # source kept


def test_low_confidence_marker_is_configurable() -> None:
    events, _ = run([raw(confidence="low")], EventRules(low_confidence_marker="[guess]"))
    assert events[0].summary.endswith("[guess]")


def test_low_confidence_can_be_rejected_outright() -> None:
    events, rejections = run([raw(confidence="low")], EventRules(reject_low_confidence=True))
    assert events == []
    assert "confidence" in rejections[0].reason


def test_rejecting_low_confidence_keeps_high_confidence_events() -> None:
    events, rejections = run(
        [raw(), raw(start="2026-06-07T08:00:00", end="2026-06-07T14:00:00", confidence="low")],
        EventRules(reject_low_confidence=True),
    )
    assert len(events) == 1 and len(rejections) == 1


def test_confidence_does_not_change_event_identity() -> None:
    """A confidence flicker between runs must not orphan a calendar entry."""
    high, _ = run([raw()])
    low, _ = run([raw(confidence="low")])
    assert high[0].event_key == low[0].event_key
