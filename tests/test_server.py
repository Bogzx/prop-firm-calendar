import json
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ftmo_calendar.server import FeedSelection, ServerStatus, make_handler, run_sync_loop
from ftmo_calendar.sinks.ics import write_ics
from ftmo_calendar.state import PostState, State, TrackedEvent, save_state

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


def make_state() -> State:
    return State(
        posts={
            "p1": PostState(
                content_hash="h",
                last_seen="2026-06-09T00:00:00+00:00",
                events=[
                    TrackedEvent(
                        event_key="abc123",
                        google_event_id="g1",
                        end="2026-06-06T14:00:00+03:00",
                        summary="FTMO Platform Maintenance",
                        start="2026-06-06T08:00:00+03:00",
                        event_type="maintenance",
                    ),
                    TrackedEvent(
                        event_key="cr1",
                        google_event_id="g2",
                        end="2026-06-07T12:00:00+03:00",
                        summary="Crypto Market Closed",
                        start="2026-06-07T10:00:00+03:00",
                        event_type="crypto_closure",
                    ),
                ],
            )
        }
    )


@pytest.fixture
def server(tmp_path: Path):
    state = make_state()
    state_path = tmp_path / "state.json"
    save_state(state, state_path)
    ics_path = tmp_path / "feed.ics"
    write_ics(state, ics_path, (60,), now=NOW)

    # Freshness is now part of health, so the status needs a pinned clock:
    # without one every fixture-dated run would read as weeks stale.
    status = ServerStatus(started_at=NOW.isoformat(), interval_seconds=3600, clock=lambda: NOW)

    renders: list[frozenset[str] | None] = []

    def feed_renderer(selection: FeedSelection) -> bytes:
        from ftmo_calendar.sinks.ics import render_ics
        from ftmo_calendar.state import load_state

        renders.append(selection.types)
        return render_ics(
            load_state(state_path),
            (60,),
            types=selection.types,
            firms=selection.firms,
            now=NOW,
        ).encode("utf-8")

    from ftmo_calendar.stats import StatsStore

    handler = make_handler(
        ics_path=ics_path,
        state_path=state_path,
        status=status,
        feed_renderer=feed_renderer,
        stats=StatsStore(tmp_path / "stats.json"),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, status, ics_path, renders
    httpd.shutdown()


def get(url: str):
    try:
        response = urllib.request.urlopen(url, timeout=5)
        return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def test_healthz(server) -> None:
    base, status, _, _ = server
    status.record_success(now=NOW)
    code, ctype, body = get(f"{base}/healthz")
    assert code == 200 and "application/json" in ctype
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["runs_ok"] == 1
    assert payload["last_run"] == NOW.isoformat()


def test_healthz_reports_failure(server) -> None:
    base, status, _, _ = server
    status.record_failure(RuntimeError("boom"), now=NOW)
    code, _, body = get(f"{base}/healthz")
    payload = json.loads(body)
    assert payload["ok"] is False
    assert "boom" in payload["last_error"]
    # 503, not 200: docs/DEPLOYMENT.md tells operators to point UptimeRobot
    # here, and a monitor can only page on a status code.
    assert code == 503
    assert payload["status"] == "error"


def test_healthz_503_when_sync_is_overdue(server) -> None:
    """A sync that stopped running weeks ago must not report healthy.

    This is the failure the endpoint exists for: the process is up, the feed
    still serves, nothing errored — and the calendar has quietly frozen.
    """
    base, status, _, _ = server
    status.record_success(now=NOW)
    assert get(f"{base}/healthz")[0] == 200

    # Interval is 3600s, so the stale threshold is 2h. Jump the clock past it.
    status.clock = lambda: NOW + timedelta(hours=3)
    code, _, body = get(f"{base}/healthz")
    payload = json.loads(body)
    assert code == 503
    assert payload["ok"] is False and payload["stale"] is True
    assert payload["status"] == "stale"
    assert payload["last_error"] is None  # nothing raised; it simply stopped
    assert payload["last_success_age_seconds"] == 3 * 3600


def test_healthz_tolerates_one_missed_interval(server) -> None:
    """One slow or retried run must not page anyone."""
    base, status, _, _ = server
    status.record_success(now=NOW)
    status.clock = lambda: NOW + timedelta(minutes=90)  # < 2 intervals
    assert get(f"{base}/healthz")[0] == 200


def test_a_freshly_started_server_is_not_stale(server) -> None:
    """Before the first sync lands, grace is measured from startup."""
    base, status, _, _ = server
    assert status.last_success is None
    code, _, body = get(f"{base}/healthz")
    payload = json.loads(body)
    assert code == 200
    # …but the grace window is not a success. Reporting an age here would tell
    # a monitor the feed had just synced when it never has.
    assert payload["last_success"] is None
    assert payload["last_success_age_seconds"] is None


def test_startup_grace_still_expires(server) -> None:
    """A process that comes up and never syncs must not stay green forever."""
    base, status, _, _ = server
    status.clock = lambda: NOW + timedelta(hours=3)
    payload = json.loads(get(f"{base}/healthz")[2])
    assert payload["ok"] is False and payload["stale"] is True
    assert payload["last_success"] is None


def test_persistent_failure_never_looks_fresh(server) -> None:
    """Age is measured from the last success, not the last attempt."""
    base, status, _, _ = server
    status.record_success(now=NOW)
    for minute in range(0, 200, 20):  # failing on schedule for >2 intervals
        status.record_failure(RuntimeError("scrape failed"), now=NOW + timedelta(minutes=minute))
    status.clock = lambda: NOW + timedelta(minutes=200)
    payload = json.loads(get(f"{base}/healthz")[2])
    assert payload["ok"] is False and payload["stale"] is True


def test_healthz_503_on_anomaly(server) -> None:
    """A run that completed but produced a suspicious result is not healthy."""
    base, status, _, _ = server
    status.record_success(now=NOW, anomalies=["keyword gate matched none of 4 scraped post(s)"])
    code, _, body = get(f"{base}/healthz")
    payload = json.loads(body)
    assert code == 503
    assert payload["status"] == "anomaly"
    assert payload["last_error"] is None
    assert "keyword gate" in payload["anomalies"][0]


def test_a_later_clean_run_clears_the_anomaly(server) -> None:
    base, status, _, _ = server
    status.record_success(now=NOW, anomalies=["something odd"])
    status.record_success(now=NOW)
    assert get(f"{base}/healthz")[0] == 200


def test_filtered_feed_is_cached_per_type_set(server) -> None:
    """Re-rendering the whole calendar (incl. VTIMEZONE bisection) per request
    is unnecessary work on an unbounded thread-per-connection server."""
    base, _, _, renders = server
    first = get(f"{base}/feed.ics?types=crypto_closure")[2]
    for _ in range(5):
        assert get(f"{base}/feed.ics?types=crypto_closure")[2] == first
    assert renders == [frozenset({"crypto_closure"})]  # rendered exactly once

    get(f"{base}/feed.ics?types=maintenance")  # a different filter still renders
    assert len(renders) == 2


def test_filtered_feed_cache_follows_state_changes(server, tmp_path: Path) -> None:
    """A completed sync must be visible on the next request, not after a restart."""
    base, _, _, renders = server
    get(f"{base}/feed.ics?types=maintenance")
    assert len(renders) == 1

    state_path = tmp_path / "state.json"
    state = make_state()
    state.posts["p1"].events[0].summary = "Rescheduled Maintenance"
    save_state(state, state_path)

    body = get(f"{base}/feed.ics?types=maintenance")[2]
    assert len(renders) == 2
    assert b"Rescheduled Maintenance" in body


def test_feed_served_as_calendar(server) -> None:
    base, _, _, _ = server
    code, ctype, body = get(f"{base}/feed.ics")
    assert code == 200
    assert "text/calendar" in ctype
    assert b"BEGIN:VCALENDAR" in body


def test_feed_filtered_by_type(server) -> None:
    base, _, _, _ = server
    code, ctype, body = get(f"{base}/feed.ics?types=crypto_closure")
    assert code == 200 and "text/calendar" in ctype
    assert b"Crypto Market Closed" in body
    assert b"Platform Maintenance" not in body


def test_feed_filter_combines_types(server) -> None:
    body = get(f"{server[0]}/feed.ics?types=crypto_closure,maintenance")[2]
    assert b"Crypto Market Closed" in body and b"FTMO Platform Maintenance" in body


def test_feed_unknown_type_is_400(server) -> None:
    code, _, body = get(f"{server[0]}/feed.ics?types=bogus")
    assert code == 400
    assert b"crypto_closure" in body  # the error lists valid types


def test_feed_404_when_missing(server) -> None:
    base, _, ics_path, _ = server
    ics_path.unlink()
    code, _, _ = get(f"{base}/feed.ics")
    assert code == 404


def test_status_page_lists_events(server) -> None:
    base, _, _, _ = server
    code, ctype, body = get(f"{base}/status")
    assert code == 200 and "text/html" in ctype
    assert b"FTMO Platform Maintenance" in body


def test_landing_page_has_subscribe_and_countdown(server) -> None:
    body = get(f"{server[0]}/")[2].decode("utf-8")
    assert 'id="count"' in body  # countdown element
    assert 'id="feedurl"' in body  # copyable feed URL
    assert "GOOGLE CALENDAR" in body and "APPLE CALENDAR" in body and "OUTLOOK" in body
    assert 'data-iso="2026-06-06T08:00:00+03:00"' in body  # local-tz upgrade hooks
    assert 'data-type="crypto_closure"' in body  # feed filter chips


def test_unknown_path_404(server) -> None:
    assert get(f"{server[0]}/nope")[0] == 404


def test_security_headers_present(server) -> None:
    response = urllib.request.urlopen(f"{server[0]}/", timeout=5)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Python" not in (response.headers.get("Server") or "")


def test_page_sets_visitor_cookie_once(server) -> None:
    base, _, _, _ = server
    response = urllib.request.urlopen(f"{base}/", timeout=5)
    set_cookie = response.headers.get("Set-Cookie", "")
    assert "aftc_id=" in set_cookie and "HttpOnly" in set_cookie
    cookie_value = set_cookie.split(";", 1)[0]
    request = urllib.request.Request(f"{base}/", headers={"Cookie": cookie_value})
    second = urllib.request.urlopen(request, timeout=5)
    assert second.headers.get("Set-Cookie") is None  # known visitor: no new cookie


def test_stats_endpoint_counts_visits_and_feed_pulls(server) -> None:
    base, _, _, _ = server
    cookie = urllib.request.urlopen(f"{base}/", timeout=5).headers["Set-Cookie"].split(";")[0]
    request = urllib.request.Request(f"{base}/", headers={"Cookie": cookie})
    urllib.request.urlopen(request, timeout=5)  # same visitor again
    get(f"{base}/feed.ics")
    payload = json.loads(get(f"{base}/stats")[2])
    assert payload["today"]["views"] >= 2
    assert payload["today"]["visitors"] >= 1
    assert payload["today"]["feed_hits"] >= 1


def test_page_footer_shows_todays_stats(server) -> None:
    base, _, _, _ = server
    body = get(f"{base}/")[2].decode("utf-8")
    assert "feed pulls" in body and "no third-party trackers" in body


def test_sync_loop_runs_and_records() -> None:
    status = ServerStatus(started_at=NOW.isoformat())
    stop = threading.Event()
    calls = []

    def sync() -> None:
        calls.append(1)
        if len(calls) >= 3:
            stop.set()

    run_sync_loop(sync, interval_seconds=0.01, stop=stop, status=status)
    assert len(calls) == 3
    assert status.snapshot()["runs_ok"] == 3


def test_sync_loop_records_failure_and_continues() -> None:
    status = ServerStatus(started_at=NOW.isoformat())
    stop = threading.Event()
    errors = []
    calls = []

    def sync() -> None:
        calls.append(1)
        if len(calls) >= 2:
            stop.set()
        raise RuntimeError("scrape failed")

    run_sync_loop(sync, interval_seconds=0.01, stop=stop, status=status, on_error=errors.append)
    assert len(calls) == 2
    snapshot = status.snapshot()
    assert snapshot["runs_failed"] == 2
    assert "scrape failed" in snapshot["last_error"]
    # Identical persistent error: notified ONCE, not every interval.
    assert len(errors) == 1


def test_sync_loop_notifies_distinct_and_recurring_errors() -> None:
    status = ServerStatus(started_at=NOW.isoformat())
    stop = threading.Event()
    errors = []
    script = [RuntimeError("error A"), None, RuntimeError("error A"), RuntimeError("error B")]

    def sync() -> None:
        step = script.pop(0)
        if not script:
            stop.set()
        if step is not None:
            raise step

    run_sync_loop(sync, interval_seconds=0.01, stop=stop, status=status, on_error=errors.append)
    # A (new), success resets, A again (new after reset), B (different)
    assert [str(e) for e in errors] == ["error A", "error A", "error B"]


def test_sync_loop_records_anomalies_from_a_run_that_did_not_raise() -> None:
    status = ServerStatus(started_at=NOW.isoformat(), clock=lambda: NOW)
    stop = threading.Event()

    def sync() -> list[str]:
        stop.set()
        return ["keyword gate matched none of 4 scraped post(s)"]

    run_sync_loop(sync, interval_seconds=0.01, stop=stop, status=status)
    snapshot = status.snapshot()
    assert snapshot["runs_ok"] == 1  # it did not fail…
    assert snapshot["ok"] is False  # …and it is still not healthy
    assert "keyword gate" in snapshot["anomalies"][0]


def test_sync_loop_accepts_a_sync_that_returns_nothing() -> None:
    """The Callable[[], None] contract still works."""
    status = ServerStatus(started_at=NOW.isoformat(), clock=lambda: NOW)
    stop = threading.Event()

    def sync() -> None:
        stop.set()

    run_sync_loop(sync, interval_seconds=0.01, stop=stop, status=status)
    assert status.snapshot()["ok"] is True


def test_check_writable_rejects_an_unusable_data_dir(tmp_path: Path) -> None:
    """The Docker volume ownership trap: uid 1000 against a bind mount it does
    not own persists nothing while every healthcheck passes."""
    from ftmo_calendar.server import DataDirError, check_writable

    blocker = tmp_path / "data"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    with pytest.raises(DataDirError, match="chown -R 1000:1000"):
        check_writable(blocker)


def test_check_writable_creates_a_missing_dir(tmp_path: Path) -> None:
    from ftmo_calendar.server import check_writable

    target = tmp_path / "fresh"
    check_writable(target)
    assert target.is_dir()
    assert not any(target.iterdir())  # the probe file is cleaned up


def test_healthz_includes_next_run(server) -> None:
    base, status, _, _ = server
    status.record_success(now=NOW)
    payload = json.loads(get(f"{base}/healthz")[2])
    assert payload["next_run"] == "2026-06-09T13:00:00+00:00"  # NOW + 3600s interval


def test_status_page_shows_next_event(server) -> None:
    base, _, _, _ = server
    body = get(f"{base}/status")[2].decode("utf-8")
    assert "NEXT TRADING INTERRUPTION" in body
