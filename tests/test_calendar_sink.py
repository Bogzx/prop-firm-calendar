from datetime import datetime
from zoneinfo import ZoneInfo

from prop_firm_calendar.models import EventType, TradingEvent
from prop_firm_calendar.sinks.google_calendar import PRIVATE_KEY_PROP, build_event_body

TZ = ZoneInfo("Europe/Bucharest")
EVENT = TradingEvent(
    event_type=EventType.MAINTENANCE,
    summary="⚠️ FTMO Platform Maintenance",
    description="details…",
    start=datetime(2026, 6, 6, 8, 0, tzinfo=TZ),
    end=datetime(2026, 6, 6, 14, 0, tzinfo=TZ),
    source_post_key="trading-update-2026-06-04",
    source_url="https://ftmo.com/en/trading-updates/",
)


def test_event_body_has_times_and_zone() -> None:
    body = build_event_body(EVENT, "Europe/Bucharest", (60, 10))
    assert body["start"] == {
        "dateTime": "2026-06-06T08:00:00+03:00",
        "timeZone": "Europe/Bucharest",
    }
    assert body["end"]["dateTime"] == "2026-06-06T14:00:00+03:00"


def test_event_body_sets_reminders() -> None:
    body = build_event_body(EVENT, "Europe/Bucharest", (60, 10))
    assert body["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 60}, {"method": "popup", "minutes": 10}],
    }


def test_event_body_carries_reconcile_key() -> None:
    body = build_event_body(EVENT, "Europe/Bucharest", ())
    assert body["extendedProperties"]["private"][PRIVATE_KEY_PROP] == EVENT.event_key
    assert body["reminders"] == {"useDefault": True}
