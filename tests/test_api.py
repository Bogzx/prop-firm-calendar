"""The public JSON API: /api/v1/events and /api/v1/next."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from prop_firm_calendar import api
from prop_firm_calendar.models import EventType
from prop_firm_calendar.server import ServerStatus, make_handler
from prop_firm_calendar.state import PostState, State, TrackedEvent, save_state

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
TITLES = {"ftmo": "FTMO", "topstep": "Topstep", "e8-markets": "E8 Markets"}
URLS = {
    "ftmo": "https://ftmo.com/en/trading-updates/",
    "topstep": "https://help.topstep.com/en/articles/13350348",
    "e8-markets": "https://help.e8markets.com/en/articles/12122593",
}
CATALOG = api.Catalog(
    firms=["ftmo", "topstep", "e8-markets"],
    types=sorted(t.value for t in EventType),
    titles=TITLES,
    urls=URLS,
    default_firm="ftmo",
)


def ev(
    key: str, start: str, end: str, kind: str = "early_close", summary: str = ""
) -> TrackedEvent:
    return TrackedEvent(
        event_key=key,
        google_event_id=f"g-{key}",
        start=start,
        end=end,
        summary=summary or f"⏳ Early Close — {key}",
        event_type=kind,
    )


def make_state() -> State:
    past = ev("past", "2026-09-01T08:00:00+03:00", "2026-09-01T09:00:00+03:00", "maintenance")
    live = ev("live", "2026-09-07T14:00:00+03:00", "2026-09-07T23:59:00+03:00")  # 11:00Z–20:59Z
    later = ev("later", "2026-09-26T03:00:00+03:00", "2026-09-26T05:00:00+03:00", "maintenance")
    twin = ev("twin", live.start, live.end, summary=live.summary)  # same window, second post
    topstep = ev("ts", "2026-11-26T11:45:00-06:00", "2026-11-26T23:59:00-06:00")
    return State(
        posts={
            "trading-update-2026-09-01": PostState(
                "h",
                NOW.isoformat(),
                [past, live, later],
                firm="ftmo",
                url="https://ftmo.com/en/blog/trading-updates/x/",
            ),
            "trading-update-2026-09-03": PostState("h", NOW.isoformat(), [twin], firm="ftmo"),
            "topstep-holiday": PostState("h", NOW.isoformat(), [topstep], firm="topstep"),
        }
    )


# -- the projection --------------------------------------------------------


def test_a_bare_request_lists_what_is_live_or_coming_up() -> None:
    body = api.events(make_state(), CATALOG, {}, NOW)
    assert [e["id"] for e in body["events"]] == ["live", "later", "ts"]
    assert [e["status"] for e in body["events"]] == ["live", "upcoming", "upcoming"]
    assert body["count"] == 3
    assert body["filters"]["from"] == NOW.isoformat()


def test_a_window_announced_twice_is_one_row() -> None:
    ids = [e["id"] for e in api.event_rows(make_state(), CATALOG, NOW)]
    assert "twin" not in ids and "live" in ids


def test_rows_carry_firm_times_and_their_own_source() -> None:
    rows = {e["id"]: e for e in api.event_rows(make_state(), CATALOG, NOW)}
    ts = rows["ts"]
    assert ts["firm"] == "topstep" and ts["firm_name"] == "Topstep"
    assert ts["start"] == "2026-11-26T11:45:00-06:00"  # as announced
    assert ts["start_utc"] == "2026-11-26T17:45:00+00:00"
    assert ts["source_url"] == URLS["topstep"]  # no post URL recorded: firm's page
    assert rows["live"]["source_url"] == "https://ftmo.com/en/blog/trading-updates/x/"


def test_filters_by_firm_type_and_time() -> None:
    state = make_state()
    by_firm = api.events(state, CATALOG, {"firm": "topstep"}, NOW)
    assert [e["id"] for e in by_firm["events"]] == ["ts"]
    by_type = api.events(state, CATALOG, {"type": "maintenance"}, NOW)
    assert [e["id"] for e in by_type["events"]] == ["later"]
    window = api.events(state, CATALOG, {"from": "2026-08-30", "to": "2026-09-08"}, NOW)
    assert [e["id"] for e in window["events"]] == ["past", "live"]
    # Overlap, not containment: a window already running at `from` is included.
    running = api.events(state, CATALOG, {"from": "2026-09-07T15:00:00Z"}, NOW)
    assert running["events"][0]["id"] == "live"


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"firm": "nope"}, "unknown firm"),
        ({"type": "lunch"}, "unknown type"),
        ({"from": "next tuesday"}, "from: expected an ISO 8601"),
        ({"from": "2026-09-10", "to": "2026-09-01"}, "'to' must be after 'from'"),
    ],
)
def test_bad_parameters_are_explained(params: dict, needle: str) -> None:
    with pytest.raises(api.ApiError, match=needle):
        api.events(make_state(), CATALOG, params, NOW)


def test_next_is_per_firm_and_says_null_for_a_clear_calendar() -> None:
    body = api.next_windows(make_state(), CATALOG, {}, NOW)
    by_firm = {f["firm"]: f["next"] for f in body["firms"]}
    assert list(by_firm) == ["ftmo", "topstep", "e8-markets"]
    assert by_firm["ftmo"]["id"] == "live"  # in progress beats the next one
    assert by_firm["topstep"]["id"] == "ts"
    assert by_firm["e8-markets"] is None


def test_next_can_be_narrowed_to_a_type() -> None:
    body = api.next_windows(make_state(), CATALOG, {"firm": "ftmo", "type": "maintenance"}, NOW)
    assert [(f["firm"], f["next"]["id"]) for f in body["firms"]] == [("ftmo", "later")]


def test_pre_multifirm_state_belongs_to_the_default_firm() -> None:
    state = State(posts={"old": PostState("h", NOW.isoformat(), [make_state_first_live()])})
    [row] = api.event_rows(state, CATALOG, NOW)
    assert row["firm"] == "ftmo"


def make_state_first_live() -> TrackedEvent:
    return ev("legacy", "2026-09-08T08:00:00+03:00", "2026-09-08T09:00:00+03:00")


# -- over HTTP ---------------------------------------------------------------


@pytest.fixture
def base(tmp_path: Path):
    state_path = tmp_path / "state.json"
    save_state(make_state(), state_path)
    status = ServerStatus(started_at=NOW.isoformat(), interval_seconds=3600, clock=lambda: NOW)
    handler = make_handler(
        ics_path=tmp_path / "feed.ics",
        state_path=state_path,
        status=status,
        valid_firms=CATALOG.firms,
        firm_titles=TITLES,
        firm_urls=URLS,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def request(url: str, method: str = "GET", headers: dict | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as response:  # noqa: S310 - test-local http
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers), e.read()


def test_events_over_http_with_cors_and_caching(base: str) -> None:
    code, headers, body = request(f"{base}/api/v1/events?firm=ftmo,topstep")
    assert code == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert headers["Cache-Control"] == "public, max-age=300"
    assert headers["ETag"].startswith('"')
    assert "Set-Cookie" not in headers
    payload = json.loads(body)
    assert [e["id"] for e in payload["events"]] == ["live", "later", "ts"]


def test_a_matching_etag_is_a_304(base: str) -> None:
    _, headers, _ = request(f"{base}/api/v1/next")
    code, again, body = request(f"{base}/api/v1/next", headers={"If-None-Match": headers["ETag"]})
    assert code == 304 and body == b""
    assert again["ETag"] == headers["ETag"]
    assert again["Access-Control-Allow-Origin"] == "*"


def test_a_bad_request_is_a_400_naming_the_valid_values(base: str) -> None:
    code, headers, body = request(f"{base}/api/v1/events?firm=ftmo,nope")
    assert code == 400
    assert headers["Cache-Control"] == "no-store"
    payload = json.loads(body)
    assert "nope" in payload["error"]
    assert payload["valid"] == sorted(CATALOG.firms)


def test_the_index_lists_endpoints_firms_and_types(base: str) -> None:
    code, _, body = request(f"{base}/api/v1/")
    payload = json.loads(body)
    assert code == 200
    assert {f["firm"] for f in payload["firms"]} == set(CATALOG.firms)
    assert "early_close" in payload["types"]
    assert "/api/v1/events" in payload["endpoints"]


def test_an_unknown_api_path_is_a_json_404(base: str) -> None:
    code, headers, body = request(f"{base}/api/v2/events")
    assert code == 404 and json.loads(body)["error"] == "not found"
    assert headers["Access-Control-Allow-Origin"] == "*"


def test_cors_preflight(base: str) -> None:
    code, headers, _ = request(f"{base}/api/v1/events", method="OPTIONS")
    assert code == 204
    assert headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"
    assert headers["Access-Control-Max-Age"] == "86400"
    code, _, _ = request(f"{base}/feed.ics", method="OPTIONS")
    assert code == 404  # only the API is cross-origin


def test_the_api_picks_up_a_new_sync(base: str, tmp_path: Path) -> None:
    _, _, before = request(f"{base}/api/v1/events?firm=e8-markets")
    assert json.loads(before)["count"] == 0
    state = make_state()
    state.posts["e8-schedule-2026-09-01"] = PostState(
        "h",
        NOW.isoformat(),
        [ev("e8", "2026-09-07T20:00:00+03:00", "2026-09-07T23:59:00+03:00")],
        firm="e8-markets",
    )
    save_state(state, tmp_path / "state.json")
    _, _, after = request(f"{base}/api/v1/events?firm=e8-markets")
    assert [e["id"] for e in json.loads(after)["events"]] == ["e8"]
