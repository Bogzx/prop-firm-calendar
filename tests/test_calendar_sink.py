from datetime import datetime
from zoneinfo import ZoneInfo

import httplib2  # a google-api-python-client dependency
import pytest
from googleapiclient.errors import HttpError

import prop_firm_calendar.sinks.google_calendar as gcal
from prop_firm_calendar.config import CalendarConfig
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


# -- the sink against a fake Google service --------------------------------


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


class _Call:
    def __init__(self, result) -> None:  # noqa: ANN001
        self.result = result

    def execute(self):  # noqa: ANN202
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeService:
    """Just enough of the Calendar v3 client for GoogleCalendarSink."""

    def __init__(self, calendars=(), events_by_key=None, fail=None) -> None:  # noqa: ANN001
        self.calendars_list = [{"id": f"cal-{n}", "summary": n} for n in calendars]
        self.events_by_key = dict(events_by_key or {})
        self.fail = fail or {}
        self.inserted: list[tuple[str, dict]] = []
        self.deleted: list[tuple[str, str]] = []
        self.created_calendars: list[dict] = []
        self.list_queries: list[dict] = []

    def calendarList(self):  # noqa: ANN202, N802 - Google's naming
        outer = self

        class _List:
            def list(self):  # noqa: ANN202
                return _Call(outer.fail.get("calendarList") or {"items": outer.calendars_list})

        return _List()

    def calendars(self):  # noqa: ANN202
        outer = self

        class _Calendars:
            def insert(self, body):  # noqa: ANN001, ANN202
                outer.created_calendars.append(body)
                return _Call({"id": "cal-new"})

        return _Calendars()

    def events(self):  # noqa: ANN202
        outer = self

        class _Events:
            def list(self, **query):  # noqa: ANN003, ANN202
                outer.list_queries.append(query)
                key = query["privateExtendedProperty"].split("=", 1)[1]
                found = outer.events_by_key.get(key)
                return _Call(outer.fail.get("list") or {"items": [{"id": found}] if found else []})

            def insert(self, calendarId, body):  # noqa: ANN001, ANN202, N803
                outer.inserted.append((calendarId, body))
                return _Call(outer.fail.get("insert") or {"id": f"ev-{len(outer.inserted)}"})

            def delete(self, calendarId, eventId):  # noqa: ANN001, ANN202, N803
                outer.deleted.append((calendarId, eventId))
                return _Call(outer.fail.get("delete") or {})

        return _Events()


def make_sink(
    monkeypatch: pytest.MonkeyPatch, service: FakeService, **cfg
) -> gcal.GoogleCalendarSink:
    monkeypatch.setattr(gcal, "build", lambda *a, **k: service)
    return gcal.GoogleCalendarSink(object(), CalendarConfig(**cfg))


def test_an_existing_named_calendar_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(calendars=["Personal", "Trading"])
    assert make_sink(monkeypatch, service).calendar_id == "cal-Trading"
    assert service.created_calendars == []


def test_a_missing_calendar_is_created_in_the_configured_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(calendars=["Personal"])
    sink = make_sink(monkeypatch, service, timezone="Europe/Prague")
    assert sink.calendar_id == "cal-new"
    assert service.created_calendars == [{"summary": "Trading", "timeZone": "Europe/Prague"}]


def test_an_explicit_calendar_id_skips_the_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(fail={"calendarList": http_error(500)})
    assert make_sink(monkeypatch, service, calendar_id="abc@group").calendar_id == "abc@group"


def test_calendar_lookup_failure_is_a_sink_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(gcal.CalendarSinkError, match="lookup/creation failed"):
        make_sink(monkeypatch, FakeService(fail={"calendarList": http_error(403)}))


def test_create_and_find_by_reconcile_key(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(calendars=["Trading"], events_by_key={EVENT.event_key: "ev-old"})
    sink = make_sink(monkeypatch, service)
    assert sink.find_event_id_by_key(EVENT.event_key) == "ev-old"
    assert (
        service.list_queries[0]["privateExtendedProperty"]
        == f"{PRIVATE_KEY_PROP}={EVENT.event_key}"
    )
    assert sink.find_event_id_by_key("unknown") is None
    assert sink.create_event(EVENT) == "ev-1"
    calendar_id, body = service.inserted[0]
    assert calendar_id == "cal-Trading" and body["summary"] == EVENT.summary


def test_deleting_an_already_gone_event_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared window's entry may have been removed by hand; that is not an error."""
    for status in (404, 410):
        sink = make_sink(
            monkeypatch, FakeService(calendars=["Trading"], fail={"delete": http_error(status)})
        )
        sink.delete_event("ev-x")  # no exception


@pytest.mark.parametrize("op", ["list", "insert", "delete"])
def test_other_api_errors_are_sink_errors(monkeypatch: pytest.MonkeyPatch, op: str) -> None:
    sink = make_sink(monkeypatch, FakeService(calendars=["Trading"], fail={op: http_error(500)}))
    with pytest.raises(gcal.CalendarSinkError):
        if op == "list":
            sink.find_event_id_by_key("k")
        elif op == "insert":
            sink.create_event(EVENT)
        else:
            sink.delete_event("ev-x")
