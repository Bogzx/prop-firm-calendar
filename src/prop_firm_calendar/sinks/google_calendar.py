"""Google Calendar sink with reconcile-key support."""

from __future__ import annotations

import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from prop_firm_calendar.config import CalendarConfig
from prop_firm_calendar.models import TradingEvent

logger = logging.getLogger(__name__)

PRIVATE_KEY_PROP = "aftc_key"


class CalendarSinkError(Exception):
    """A Google Calendar API operation failed."""


def build_event_body(
    event: TradingEvent, timezone_name: str, reminders_minutes: tuple[int, ...]
) -> dict:
    reminders: dict = {"useDefault": True}
    if reminders_minutes:
        reminders = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in reminders_minutes],
        }
    return {
        "summary": event.summary,
        "description": event.description,
        "start": {"dateTime": event.start.isoformat(), "timeZone": timezone_name},
        "end": {"dateTime": event.end.isoformat(), "timeZone": timezone_name},
        "reminders": reminders,
        "extendedProperties": {"private": {PRIVATE_KEY_PROP: event.event_key}},
    }


class GoogleCalendarSink:
    def __init__(self, credentials, cfg: CalendarConfig) -> None:  # noqa: ANN001
        self._cfg = cfg
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self.calendar_id = self._resolve_calendar()

    def _resolve_calendar(self) -> str:
        if self._cfg.calendar_id:
            return self._cfg.calendar_id
        # oauth mode: find or create the named calendar
        try:
            page = self._service.calendarList().list().execute()
            for entry in page.get("items", []):
                if entry.get("summary") == self._cfg.name:
                    logger.info("Using existing calendar '%s'", self._cfg.name)
                    return entry["id"]
            created = (
                self._service.calendars()
                .insert(body={"summary": self._cfg.name, "timeZone": self._cfg.timezone})
                .execute()
            )
            logger.info("Created calendar '%s'", self._cfg.name)
            return created["id"]
        except HttpError as e:
            raise CalendarSinkError(f"calendar lookup/creation failed: {e}") from e

    def find_event_id_by_key(self, event_key: str) -> str | None:
        try:
            result = (
                self._service.events()
                .list(
                    calendarId=self.calendar_id,
                    privateExtendedProperty=f"{PRIVATE_KEY_PROP}={event_key}",
                    maxResults=1,
                    singleEvents=True,
                )
                .execute()
            )
        except HttpError as e:
            raise CalendarSinkError(f"event lookup failed: {e}") from e
        items = result.get("items", [])
        return items[0]["id"] if items else None

    def create_event(self, event: TradingEvent) -> str:
        body = build_event_body(event, self._cfg.timezone, self._cfg.reminders_minutes)
        try:
            created = (
                self._service.events().insert(calendarId=self.calendar_id, body=body).execute()
            )
        except HttpError as e:
            raise CalendarSinkError(f"event creation failed: {e}") from e
        logger.info("Created event: %s", created.get("htmlLink", created.get("id")))
        return created["id"]

    def delete_event(self, event_id: str) -> None:
        try:
            self._service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as e:
            if e.resp is not None and e.resp.status in (404, 410):
                logger.info("Event %s already gone", event_id)
                return
            raise CalendarSinkError(f"event deletion failed: {e}") from e
