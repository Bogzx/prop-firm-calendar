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


# -- multi-firm UI ---------------------------------------------------------

FTMO_SRC = {
    "firm": "ftmo",
    "display_name": "FTMO",
    "ok": True,
    "status": "ok",
    "last_success": "2026-06-09T12:00:00+00:00",
    "last_success_age_seconds": 900,
}
TOPSTEP_SRC = {
    "firm": "topstep",
    "display_name": "Topstep",
    "ok": True,
    "status": "ok",
    "last_success": "2026-06-09T12:00:00+00:00",
    "last_success_age_seconds": 900,
}
SINGLE = dict(SNAPSHOT, source="FTMO", sources=[FTMO_SRC])
MULTI = dict(SNAPSHOT, source="FTMO, Topstep", sources=[FTMO_SRC, TOPSTEP_SRC])


def state_of(pairs: list[tuple[str, TrackedEvent]]) -> State:
    """One post per (firm, event) pair, so events carry firm attribution."""
    return State(
        posts={
            f"p{i}": PostState(
                content_hash=f"h{i}",
                last_seen="2026-06-09T00:00:00+00:00",
                events=[event],
                firm=firm,
            )
            for i, (firm, event) in enumerate(pairs)
        }
    )


def test_single_firm_keeps_firm_specific_branding() -> None:
    """An FTMO-only deployment must look exactly as it did before multi-firm."""
    page = render_page(State(), SINGLE).decode("utf-8")
    assert "<title>FTMO Trading Calendar — next interruption</title>" in page
    assert "FTMO TRADING CALENDAR" in page
    assert "Prop Firm" not in page


def test_multi_firm_uses_neutral_branding() -> None:
    page = render_page(State(), MULTI).decode("utf-8")
    assert "<title>Prop Firm Trading Calendar — next interruption</title>" in page
    assert "PROP FIRM TRADING CALENDAR" in page
    # the page must still name who is actually in it
    assert "FTMO · Topstep" in page


def test_multi_firm_meta_description_is_not_ftmo_specific() -> None:
    page = render_page(State(), MULTI).decode("utf-8")
    description = page.split('name="description" content="')[1].split('"')[0]
    assert "FTMO" not in description


def test_multi_firm_disclaimer_does_not_name_one_firm() -> None:
    page = render_page(State(), MULTI).decode("utf-8")
    assert "not affiliated with FTMO" not in page
    assert "not affiliated" in page


def test_multi_firm_renders_firm_chips() -> None:
    page = render_page(State(), MULTI).decode("utf-8")
    assert 'data-firm="ftmo"' in page
    assert 'data-firm="topstep"' in page


def test_single_firm_renders_no_firm_chips() -> None:
    """One firm: a filter offering exactly one choice is noise.

    Asserts no chip is *rendered* — the shared script always carries the
    data-firm selector, which is static and harmless with an empty NodeList.
    """
    assert 'type="checkbox" data-firm' not in render_page(State(), SINGLE).decode("utf-8")


def test_multi_firm_rows_carry_a_firm_badge() -> None:
    page = render_page(
        state_of(
            [
                ("ftmo", TrackedEvent("k1", "g1", end=iso(5), summary="Maintenance", start=iso(3))),
                ("topstep", TrackedEvent("k2", "g2", end=iso(6), summary="Holiday", start=iso(4))),
            ]
        ),
        MULTI,
    ).decode("utf-8")
    assert '<span class="fbadge">FTMO</span>Maintenance' in page
    assert '<span class="fbadge">Topstep</span>Holiday' in page


def test_single_firm_rows_have_no_badge() -> None:
    page = render_page(
        state_of([("ftmo", TrackedEvent("k1", "g1", end=iso(5), summary="Solo", start=iso(3)))]),
        SINGLE,
    ).decode("utf-8")
    assert '<span class="fbadge">' not in page  # the rule exists in CSS; no element uses it
    assert '<td class="ev">Solo</td>' in page


def test_legacy_events_are_attributed_to_the_default_firm() -> None:
    """Pre-multi-firm state has firm="" — it must not render a blank badge."""
    page = render_page(
        state_of([("", TrackedEvent("k1", "g1", end=iso(5), summary="Old event", start=iso(3)))]),
        MULTI,
        default_firm="ftmo",
    ).decode("utf-8")
    assert '<span class="fbadge">FTMO</span>Old event' in page
    assert '<span class="fbadge"></span>' not in page


def test_unknown_firm_falls_back_to_its_profile_name() -> None:
    page = render_page(
        state_of([("mystery", TrackedEvent("k", "g", end=iso(5), summary="X", start=iso(3)))]),
        MULTI,
    ).decode("utf-8")
    assert '<span class="fbadge">mystery</span>X' in page


def test_firm_badge_is_escaped() -> None:
    sources = [FTMO_SRC, dict(TOPSTEP_SRC, firm="x", display_name="<script>")]
    page = render_page(
        state_of([("x", TrackedEvent("k", "g", end=iso(5), summary="X", start=iso(3)))]),
        dict(MULTI, sources=sources),
    ).decode("utf-8")
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page


def test_feed_url_builder_scopes_its_selectors_per_axis() -> None:
    """Both axes share the .filters container; an unscoped selector would read
    data-type off a firm checkbox and emit '?types=null'."""
    page = render_page(State(), MULTI).decode("utf-8")
    assert ".filters input[data-type]" in page
    assert ".filters input[data-firm]" in page
    assert 'querySelectorAll(".filters input")' not in page
