"""Public read-only JSON API: the calendar as data for other tools.

    GET /api/v1/                 what is here: endpoints, firms, event types
    GET /api/v1/events           events, filterable by firm, type and time
    GET /api/v1/next             the next (or current) window for each firm

The ICS feed answers "put this in my calendar"; this answers "is it safe to
trade right now?" for a bot, an order router or a dashboard. It is a
projection of the same state file with the same de-duplication as the feed,
so the two can never disagree about what is scheduled.

Everything here is pure (state in, dict out). server.py owns HTTP concerns:
routing, CORS, caching headers and conditional requests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time

from prop_firm_calendar.state import State

VERSION = "v1"


class ApiError(ValueError):
    """A bad request: the message and `valid` values go back to the caller."""

    def __init__(self, message: str, valid: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.valid = list(valid)

    def payload(self) -> dict:
        body: dict = {"error": str(self)}
        if self.valid:
            body["valid"] = self.valid
        return body


@dataclass(frozen=True)
class Catalog:
    """What the API may be asked about: configured firms and known types."""

    firms: Sequence[str]
    types: Sequence[str]
    titles: Mapping[str, str]
    urls: Mapping[str, str]
    default_firm: str = ""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def parse_instant(raw: str, name: str) -> datetime:
    """`2026-12-24` (midnight UTC) or a full ISO 8601 timestamp (naive = UTC)."""
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        if len(text) == 10:
            return datetime.combine(datetime.fromisoformat(text).date(), time(), tzinfo=UTC)
        return _aware(datetime.fromisoformat(text))
    except ValueError as e:
        raise ApiError(f"{name}: expected an ISO 8601 date or timestamp, got {raw!r}") from e


def parse_set(raw: str, valid: Sequence[str], name: str) -> frozenset[str] | None:
    """Comma-separated values, each of which must be valid; None when absent."""
    if not raw.strip():
        return None
    values = frozenset(v.strip() for v in raw.split(",") if v.strip())
    unknown = sorted(values - set(valid))
    if unknown or not values:
        raise ApiError(f"unknown {name}: {unknown}", valid=sorted(valid))
    return values


def event_rows(state: State, catalog: Catalog, now: datetime) -> list[dict]:
    """Every tracked event as API rows, de-duplicated like the feed, by start."""
    rows: list[tuple[datetime, dict]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for post in state.posts.values():
        firm = state.firm_of(post, catalog.default_firm)
        link = post.url or catalog.urls.get(firm, "")
        for event in post.events:
            if not event.summary or not event.start:
                continue  # pre-v2 state entry without display data
            identity = (firm, event.event_type, event.start, event.end, event.summary)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                start = _aware(datetime.fromisoformat(event.start))
                end = _aware(datetime.fromisoformat(event.end))
            except ValueError:
                continue
            status = "past" if end <= now else ("live" if start <= now else "upcoming")
            row = {
                "id": event.event_key,
                "firm": firm,
                "firm_name": catalog.titles.get(firm, firm),
                "type": event.event_type,
                "summary": event.summary,
                "start": event.start,
                "end": event.end,
                "start_utc": start.astimezone(UTC).isoformat(),
                "end_utc": end.astimezone(UTC).isoformat(),
                "status": status,
                "source_url": link,
            }
            evidence = getattr(event, "evidence", "")
            if evidence:
                row["evidence"] = evidence
            rows.append((start, row))
    rows.sort(key=lambda item: (item[0], item[1]["firm"], item[1]["id"]))
    return [row for _, row in rows]


def _filters(params: Mapping[str, str], catalog: Catalog) -> tuple:
    firms = parse_set(params.get("firm", ""), catalog.firms, "firm")
    types = parse_set(params.get("type", ""), catalog.types, "type")
    return firms, types


def _selected(row: dict, firms: frozenset[str] | None, types: frozenset[str] | None) -> bool:
    return (firms is None or row["firm"] in firms) and (types is None or row["type"] in types)


def _now_iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def events(state: State, catalog: Catalog, params: Mapping[str, str], now: datetime) -> dict:
    """`/api/v1/events`: windows overlapping [from, to), filtered by firm and type.

    `from` defaults to now, so a bare request answers "what is coming up"
    (including anything in progress). Pass an earlier `from` for history —
    the state keeps roughly the last 45 days.
    """
    firms, types = _filters(params, catalog)
    start = parse_instant(params["from"], "from") if params.get("from") else now
    end = parse_instant(params["to"], "to") if params.get("to") else None
    if end is not None and end <= start:
        raise ApiError("'to' must be after 'from'")
    selected = []
    for row in event_rows(state, catalog, now):
        if not _selected(row, firms, types):
            continue
        row_start = datetime.fromisoformat(row["start_utc"])
        row_end = datetime.fromisoformat(row["end_utc"])
        if row_end <= start or (end is not None and row_start >= end):
            continue
        selected.append(row)
    return {
        "api": VERSION,
        "generated_at": _now_iso(now),
        "filters": {
            "firm": sorted(firms) if firms else None,
            "type": sorted(types) if types else None,
            "from": start.astimezone(UTC).isoformat(),
            "to": end.astimezone(UTC).isoformat() if end else None,
        },
        "count": len(selected),
        "events": selected,
    }


def next_windows(state: State, catalog: Catalog, params: Mapping[str, str], now: datetime) -> dict:
    """`/api/v1/next`: per firm, the window in progress or the next one to start.

    Every requested firm appears, with `next: null` when nothing is scheduled —
    an absent firm and a firm with a clear calendar must not look the same.
    """
    firms, types = _filters(params, catalog)
    wanted = [f for f in catalog.firms if firms is None or f in firms]
    upcoming: dict[str, dict] = {}
    for row in event_rows(state, catalog, now):
        if row["status"] == "past" or not _selected(row, None, types):
            continue
        upcoming.setdefault(row["firm"], row)  # rows are sorted by start
    return {
        "api": VERSION,
        "generated_at": _now_iso(now),
        "firms": [
            {"firm": f, "firm_name": catalog.titles.get(f, f), "next": upcoming.get(f)}
            for f in wanted
        ],
    }


def index(catalog: Catalog, now: datetime) -> dict:
    return {
        "api": VERSION,
        "generated_at": _now_iso(now),
        "endpoints": {
            "/api/v1/events": "?firm=&type=&from=&to= (comma-separated firm/type; ISO dates)",
            "/api/v1/next": "?firm=&type= — the current or next window per firm",
        },
        "firms": [{"firm": f, "firm_name": catalog.titles.get(f, f)} for f in catalog.firms],
        "types": list(catalog.types),
    }
