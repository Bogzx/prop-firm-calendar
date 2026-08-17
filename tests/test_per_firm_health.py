"""Per-firm health: a source that has gone quiet must be individually visible.

The failure this prevents is arithmetic. Nine firms publishing and one that
silently stopped average to "mostly fine", and a green badge over a feed that
has lost a source is worse than no badge — it is an assurance that the thing
you are relying on is working. So every firm carries its own freshness, its own
errors and its own staleness verdict, and any unhealthy firm pulls the overall
snapshot down with it.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ftmo_calendar.firms import FirmOutcome
from ftmo_calendar.server import FeedSelection, ServerStatus, make_handler, run_sync_loop
from ftmo_calendar.state import PostState, State, TrackedEvent, save_state
from ftmo_calendar.web import render_page

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
HOUR = 3600


def status_at(now: datetime = NOW, interval: float = HOUR) -> ServerStatus:
    return ServerStatus(started_at=now.isoformat(), interval_seconds=interval, clock=lambda: now)


def ok(name: str, display: str) -> FirmOutcome:
    return FirmOutcome(name=name, display_name=display, ok=True, posts_seen=2)


def broken(name: str, display: str, error: str) -> FirmOutcome:
    return FirmOutcome(name=name, display_name=display, ok=False, error=error)


# -- the snapshot ---------------------------------------------------------


def test_every_firm_appears_separately() -> None:
    status = status_at()
    status.record_success(now=NOW, firms=[ok("ftmo", "FTMO"), ok("topstep", "Topstep")])
    snapshot = status.snapshot(now=NOW)
    assert [s["firm"] for s in snapshot["sources"]] == ["ftmo", "topstep"]
    assert all(s["ok"] for s in snapshot["sources"])
    assert snapshot["ok"] is True


def test_one_broken_firm_is_not_averaged_into_an_overall_green() -> None:
    """The central promise of per-firm health."""
    status = status_at()
    status.record_success(
        now=NOW, firms=[ok("ftmo", "FTMO"), broken("topstep", "Topstep", "404 from help.topstep")]
    )
    snapshot = status.snapshot(now=NOW)
    assert snapshot["ok"] is False
    assert snapshot["status"] == "degraded"
    assert snapshot["unhealthy_sources"] == ["Topstep"]

    by_name = {s["firm"]: s for s in snapshot["sources"]}
    assert by_name["ftmo"]["ok"] is True
    assert by_name["topstep"]["ok"] is False
    assert "404" in by_name["topstep"]["last_error"]


def test_a_firms_anomaly_is_attributed_to_that_firm() -> None:
    status = status_at()
    status.record_success(
        now=NOW,
        firms=[
            ok("ftmo", "FTMO"),
            FirmOutcome(
                name="topstep",
                display_name="Topstep",
                ok=False,
                anomalies=("Topstep: keyword gate matched none of 1 scraped post(s)",),
            ),
        ],
    )
    snapshot = status.snapshot(now=NOW)
    by_name = {s["firm"]: s for s in snapshot["sources"]}
    assert by_name["topstep"]["status"] == "anomaly"
    assert by_name["ftmo"]["anomalies"] == []


def test_a_firm_that_stops_succeeding_goes_stale_on_its_own() -> None:
    """A firm whose scraper has gone quiet, while the others keep working."""
    status = ServerStatus(started_at=NOW.isoformat(), interval_seconds=HOUR, clock=lambda: NOW)
    status.record_success(now=NOW, firms=[ok("ftmo", "FTMO"), ok("topstep", "Topstep")])

    # Four hours later FTMO still syncs; Topstep has been failing since.
    later = NOW + timedelta(hours=4)
    status.clock = lambda: later
    status.record_success(
        now=later, firms=[ok("ftmo", "FTMO"), broken("topstep", "Topstep", "timeout")]
    )

    snapshot = status.snapshot(now=later)
    by_name = {s["firm"]: s for s in snapshot["sources"]}
    assert by_name["ftmo"]["last_success"] == later.isoformat()
    assert by_name["topstep"]["last_success"] == NOW.isoformat(), "must not ride on the loop"
    assert by_name["topstep"]["stale"] is True
    assert by_name["topstep"]["last_success_age_seconds"] == 4 * HOUR
    assert snapshot["ok"] is False


def test_a_recovering_firm_clears_its_error() -> None:
    status = status_at()
    status.record_success(now=NOW, firms=[broken("topstep", "Topstep", "boom")])
    status.record_success(now=NOW, firms=[ok("topstep", "Topstep")])
    snapshot = status.snapshot(now=NOW)
    assert snapshot["sources"][0]["ok"] is True
    assert snapshot["sources"][0]["last_error"] is None
    assert snapshot["sources"][0]["runs_ok"] == 1
    assert snapshot["sources"][0]["runs_failed"] == 1


def test_a_single_firm_deployment_reports_exactly_as_before() -> None:
    """No firms reported means no `sources` noise and the original verdict."""
    status = status_at()
    status.record_success(now=NOW)
    snapshot = status.snapshot(now=NOW)
    assert snapshot["sources"] == []
    assert snapshot["unhealthy_sources"] == []
    assert snapshot["ok"] is True
    assert snapshot["status"] == "ok"


def test_the_sync_loop_forwards_per_firm_outcomes() -> None:
    class Result:
        anomalies = ["Topstep: something odd"]
        outcomes = [ok("ftmo", "FTMO"), broken("topstep", "Topstep", "down")]

    status = status_at()
    stop = threading.Event()

    def sync():  # noqa: ANN202
        stop.set()
        return Result()

    run_sync_loop(sync, 0.01, stop, status)
    snapshot = status.snapshot(now=NOW)
    assert [s["firm"] for s in snapshot["sources"]] == ["ftmo", "topstep"]
    assert snapshot["ok"] is False


def test_the_sync_loop_still_accepts_a_plain_anomaly_list() -> None:
    """The original contract stays valid."""
    status = status_at()
    stop = threading.Event()

    def sync():  # noqa: ANN202
        stop.set()
        return ["something odd"]

    run_sync_loop(sync, 0.01, stop, status)
    snapshot = status.snapshot(now=NOW)
    assert snapshot["anomalies"] == ["something odd"]
    assert snapshot["sources"] == []


# -- the status page ------------------------------------------------------


def test_the_status_page_lists_each_source() -> None:
    status = status_at()
    status.record_success(
        now=NOW, firms=[ok("ftmo", "FTMO"), broken("topstep", "Topstep", "404 not found")]
    )
    page = render_page(State(), status.snapshot(now=NOW)).decode("utf-8")
    assert "SOURCES" in page
    assert "FTMO" in page and "Topstep" in page
    assert "404 not found" in page
    assert "SOURCE DOWN" in page


def test_the_status_page_is_unchanged_for_a_single_firm() -> None:
    """A one-firm deployment must not grow a sources panel it does not need."""
    status = status_at()
    status.record_success(now=NOW, firms=[ok("ftmo", "FTMO")])
    page = render_page(State(), status.snapshot(now=NOW)).decode("utf-8")
    assert "SOURCES" not in page


# -- the HTTP surface -----------------------------------------------------


def make_state() -> State:
    def event(key: str, kind: str) -> TrackedEvent:
        return TrackedEvent(
            event_key=key,
            google_event_id=f"g{key}",
            start="2026-07-01T10:00:00+03:00",
            end="2026-07-01T12:00:00+03:00",
            summary=f"event {key}",
            event_type=kind,
        )

    return State(
        posts={
            "f-1": PostState("h", NOW.isoformat(), [event("k1", "maintenance")], firm="ftmo"),
            "t-1": PostState("h", NOW.isoformat(), [event("k2", "early_close")], firm="topstep"),
        }
    )


@pytest.fixture
def server(tmp_path: Path):
    from ftmo_calendar.sinks.ics import render_ics, write_ics

    state = make_state()
    state_path = tmp_path / "state.json"
    save_state(state, state_path)
    ics_path = tmp_path / "feed.ics"
    write_ics(state, ics_path, (60,), now=NOW)

    status = status_at()
    status.record_success(now=NOW, firms=[ok("ftmo", "FTMO"), ok("topstep", "Topstep")])
    seen: list[FeedSelection] = []

    def feed_renderer(selection: FeedSelection) -> bytes:
        from ftmo_calendar.state import load_state

        seen.append(selection)
        return render_ics(
            load_state(state_path),
            (60,),
            types=selection.types,
            firms=selection.firms,
            now=NOW,
        ).encode("utf-8")

    handler = make_handler(
        ics_path=ics_path,
        state_path=state_path,
        status=status,
        feed_renderer=feed_renderer,
        valid_firms=["ftmo", "topstep"],
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", status, ics_path, seen
    httpd.shutdown()
    httpd.server_close()


def get_bytes(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url) as response:  # noqa: S310 - test-local http
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get(url: str) -> tuple[int, str]:
    code, body = get_bytes(url)
    return code, body.decode("utf-8")


def test_the_unfiltered_feed_is_served_from_disk_untouched(server) -> None:
    """The URL real subscribers already hold must not change behaviour."""
    base, _, ics_path, seen = server
    code, body = get_bytes(f"{base}/feed.ics")
    assert code == 200
    # Byte-for-byte, CRLF line endings and all — not merely equivalent.
    assert body == ics_path.read_bytes()
    assert seen == [], "no filter means no re-render"


def test_a_per_firm_feed_is_served(server) -> None:
    base, _, _, _ = server
    code, body = get(f"{base}/feed.ics?firms=topstep")
    assert code == 200
    assert body.count("BEGIN:VEVENT") == 1
    assert "UID:k2@" in body


def test_several_firms_can_be_requested(server) -> None:
    base, _, _, _ = server
    code, body = get(f"{base}/feed.ics?firms=ftmo,topstep")
    assert code == 200
    assert body.count("BEGIN:VEVENT") == 2


def test_firms_and_types_combine_over_http(server) -> None:
    base, _, _, _ = server
    code, body = get(f"{base}/feed.ics?firms=ftmo&types=maintenance")
    assert code == 200
    assert body.count("BEGIN:VEVENT") == 1
    code, body = get(f"{base}/feed.ics?firms=ftmo&types=early_close")
    assert body.count("BEGIN:VEVENT") == 0


def test_an_unknown_firm_is_a_400_naming_the_valid_ones(server) -> None:
    base, _, _, _ = server
    code, body = get(f"{base}/feed.ics?firms=nope")
    assert code == 400
    payload = json.loads(body)
    assert payload["valid"] == ["ftmo", "topstep"]


def test_filtered_feeds_are_cached_per_selection(server) -> None:
    base, _, _, seen = server
    for _ in range(3):
        get(f"{base}/feed.ics?firms=topstep")
    assert len(seen) == 1
    get(f"{base}/feed.ics?firms=ftmo")
    assert len(seen) == 2


def test_healthz_exposes_each_source(server) -> None:
    base, _, _, _ = server
    code, body = get(f"{base}/healthz")
    payload = json.loads(body)
    assert code == 200
    assert [s["firm"] for s in payload["sources"]] == ["ftmo", "topstep"]


def test_healthz_turns_503_when_one_firm_is_down(server) -> None:
    """A monitor can only page you if the status code moves."""
    base, status, _, _ = server
    status.record_success(now=NOW, firms=[ok("ftmo", "FTMO"), broken("topstep", "Topstep", "x")])
    code, body = get(f"{base}/healthz")
    assert code == 503
    payload = json.loads(body)
    assert payload["unhealthy_sources"] == ["Topstep"]
