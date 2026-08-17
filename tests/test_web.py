from datetime import UTC, datetime, timedelta

from ftmo_calendar.state import PostState, State, TrackedEvent
from ftmo_calendar.web import render_page

SNAPSHOT = {
    "ok": True,
    "last_run": "2026-06-09T12:00:00+00:00",
    "next_run": "2026-06-09T18:00:00+00:00",
    "last_error": None,
    "runs_ok": 4,
    "runs_failed": 0,
}


def state_with(events: list[TrackedEvent]) -> State:
    return State(
        posts={
            "p": PostState(content_hash="h", last_seen="2026-06-09T00:00:00+00:00", events=events)
        }
    )


def iso(delta_hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=delta_hours)).isoformat()


def test_upcoming_past_and_live_classification() -> None:
    events = [
        TrackedEvent("k1", "g1", end=iso(5), summary="Upcoming", start=iso(3)),
        TrackedEvent("k2", "g2", end=iso(1), summary="In progress", start=iso(-1)),
        TrackedEvent("k3", "g3", end=iso(-2), summary="Finished", start=iso(-4)),
    ]
    page = render_page(state_with(events), SNAPSHOT).decode("utf-8")
    assert '<tr class="soon"><td class="ev">Upcoming</td>' in page
    assert '<tr class="live"><td class="ev">In progress</td>' in page
    assert '<tr class="past"><td class="ev">Finished</td>' in page


def test_empty_state() -> None:
    page = render_page(State(), SNAPSHOT).decode("utf-8")
    assert "no events tracked yet" in page
    assert "OPERATIONAL" in page


def test_error_state_shown() -> None:
    snapshot = dict(SNAPSHOT, ok=False, last_error="token <expired>")
    page = render_page(State(), snapshot).decode("utf-8")
    assert "SYNC ERROR" in page
    assert "token &lt;expired&gt;" in page  # escaped


def test_dataless_events_skipped() -> None:
    page = render_page(
        state_with([TrackedEvent("k", "g", end="2026-06-10T00:00:00+00:00")]), SNAPSHOT
    ).decode("utf-8")
    assert "no events tracked yet" in page


# -- freshness and per-source health --------------------------------------


def test_stale_sync_is_not_shown_as_operational() -> None:
    """A three-week-old sync behind a green OPERATIONAL badge is the whole problem."""
    snapshot = dict(
        SNAPSHOT,
        ok=False,
        status="stale",
        stale=True,
        last_success="2026-05-19T12:00:00+00:00",
        last_success_age_seconds=21 * 86400,
    )
    page = render_page(State(), snapshot).decode("utf-8")
    assert "SYNC STALE" in page
    assert "OPERATIONAL" not in page
    assert "21 d ago" in page


def test_last_successful_run_age_is_shown() -> None:
    snapshot = dict(
        SNAPSHOT, last_success="2026-06-09T12:00:00+00:00", last_success_age_seconds=900
    )
    page = render_page(State(), snapshot).decode("utf-8")
    assert "last successful sync 15 min ago" in page


def test_never_synced_says_so() -> None:
    snapshot = dict(SNAPSHOT, last_success=None, last_success_age_seconds=None)
    page = render_page(State(), snapshot).decode("utf-8")
    assert "last successful sync never" in page


def test_a_started_but_never_successful_server_does_not_claim_a_recent_sync() -> None:
    """Seen live: the page read 'last successful sync 7 s ago' while the first
    and only sync had failed outright."""
    snapshot = dict(SNAPSHOT, ok=False, status="error", last_success=None)
    page = render_page(State(), snapshot).decode("utf-8")
    assert "last successful sync never" in page
    assert "ago" not in page.split("last successful sync")[1][:40]


def test_source_name_is_shown() -> None:
    page = render_page(State(), dict(SNAPSHOT, source="Other Prop Firm")).decode("utf-8")
    assert "Other Prop Firm" in page


def test_anomalies_are_surfaced_and_escaped() -> None:
    snapshot = dict(
        SNAPSHOT, ok=False, status="anomaly", anomalies=["keyword gate matched <none> of 4"]
    )
    page = render_page(State(), snapshot).decode("utf-8")
    assert "NEEDS REVIEW" in page
    assert "keyword gate matched &lt;none&gt; of 4" in page


def test_humanize_boundaries() -> None:
    from ftmo_calendar.web import _humanize

    assert _humanize(0) == "0 s"
    assert _humanize(45) == "45 s"
    assert _humanize(600) == "10 min"
    assert _humanize(7200) == "2 h"
    assert _humanize(3 * 86400) == "3 d"
    assert _humanize(-5) == "0 s"  # clock skew must not render "-1 s"
