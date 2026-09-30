from datetime import UTC, datetime
from pathlib import Path

from ftmo_calendar.sinks.ics import render_ics, write_ics
from ftmo_calendar.state import PostState, State, TrackedEvent

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
                        summary="⚠️ FTMO Platform Maintenance",
                        start="2026-06-06T08:00:00+03:00",
                        event_type="maintenance",
                    ),
                    # v1-era event without display data — must be skipped
                    TrackedEvent(
                        event_key="old", google_event_id="g2", end="2026-06-07T00:00:00+00:00"
                    ),
                ],
            )
        }
    )


def test_calendar_envelope_and_event() -> None:
    ics = render_ics(make_state(), (60, 10), now=NOW)
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.rstrip("\r\n").endswith("END:VCALENDAR")
    assert "VERSION:2.0" in ics
    assert ics.count("BEGIN:VEVENT") == 1  # the dataless event is skipped
    assert "UID:abc123@ftmo-calendar" in ics


def test_times_converted_to_utc() -> None:
    ics = render_ics(make_state(), (), now=NOW)
    assert "DTSTART:20260606T050000Z" in ics  # 08:00+03:00 -> 05:00Z
    assert "DTEND:20260606T110000Z" in ics


def test_local_times_with_tzid_in_calendar_timezone() -> None:
    ics = render_ics(make_state(), (), tz_name="Europe/Bucharest", now=NOW)
    # 08:00+03:00 must read 08:00 — the time FTMO announced — not 05:00 UTC.
    assert "DTSTART;TZID=Europe/Bucharest:20260606T080000" in ics
    assert "DTEND;TZID=Europe/Bucharest:20260606T140000" in ics
    assert "X-WR-TIMEZONE:Europe/Bucharest" in ics
    assert "DTSTART:20260606T050000Z" not in ics


def test_vtimezone_block_with_dst_rules() -> None:
    ics = render_ics(make_state(), (), tz_name="Europe/Bucharest", now=NOW)
    assert "BEGIN:VTIMEZONE" in ics
    assert "TZID:Europe/Bucharest" in ics
    # The probe year around a June 2026 event spans both EET/EEST changes.
    assert "BEGIN:DAYLIGHT" in ics and "BEGIN:STANDARD" in ics
    assert "TZOFFSETTO:+0300" in ics and "TZOFFSETTO:+0200" in ics
    assert "TZNAME:EEST" in ics and "TZNAME:EET" in ics


def test_instant_preserved_across_render_timezones() -> None:
    # 08:00+03:00 == 05:00Z == 01:00 in New York (EDT) — same instant, any zone.
    ics = render_ics(make_state(), (), tz_name="America/New_York", now=NOW)
    assert "DTSTART;TZID=America/New_York:20260606T010000" in ics


def test_fixed_offset_zone_gets_single_observance() -> None:
    # Moscow has had no DST since 2014: one STANDARD observance, equal offsets.
    ics = render_ics(make_state(), (), tz_name="Europe/Moscow", now=NOW)
    assert "DTSTART;TZID=Europe/Moscow:20260606T080000" in ics
    assert ics.count("BEGIN:STANDARD") == 1
    assert "BEGIN:DAYLIGHT" not in ics
    assert "TZOFFSETFROM:+0300" in ics and "TZOFFSETTO:+0300" in ics


def test_utc_rendering_has_no_vtimezone() -> None:
    ics = render_ics(make_state(), (), now=NOW)
    assert "BEGIN:VTIMEZONE" not in ics
    assert "X-WR-TIMEZONE" not in ics


def test_valarm_per_reminder() -> None:
    ics = render_ics(make_state(), (60, 10), now=NOW)
    assert ics.count("BEGIN:VALARM") == 2
    assert "TRIGGER:-PT60M" in ics and "TRIGGER:-PT10M" in ics


def test_summary_escaped() -> None:
    state = make_state()
    state.posts["p1"].events[0].summary = "Closed; markets, a\\b"
    ics = render_ics(state, (), now=NOW)
    assert "SUMMARY:Closed\\; markets\\, a\\\\b" in ics


def test_crlf_in_summary_cannot_inject_ics_lines() -> None:
    state = make_state()
    state.posts["p1"].events[0].summary = "evil\r\nATTENDEE:mailto:x@y"
    ics = render_ics(state, (), now=NOW)
    assert "\r\nATTENDEE" not in ics
    assert "SUMMARY:evil\\nATTENDEE:mailto:x@y" in ics


def test_crlf_line_endings() -> None:
    ics = render_ics(make_state(), (), now=NOW)
    assert "\n" not in ics.replace("\r\n", "")


def test_write_ics(tmp_path: Path) -> None:
    path = tmp_path / "feed.ics"
    write_ics(make_state(), path, (60,), now=NOW)
    assert path.read_text(encoding="utf-8").startswith("BEGIN:VCALENDAR")


def test_refresh_hints_for_subscribers() -> None:
    ics = render_ics(make_state(), (), refresh_minutes=360, now=NOW)
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT360M" in ics
    assert "X-PUBLISHED-TTL:PT360M" in ics


def test_no_refresh_hints_by_default() -> None:
    ics = render_ics(make_state(), (), now=NOW)
    assert "REFRESH-INTERVAL" not in ics


def test_description_with_source_url() -> None:
    ics = render_ics(make_state(), (), source_url="https://ftmo.com/en/trading-updates/", now=NOW)
    assert "DESCRIPTION:Source: https://ftmo.com/en/trading-updates/" in ics
    assert "AutoFtmoCalendar" in ics


def crypto_event() -> TrackedEvent:
    return TrackedEvent(
        event_key="cr1",
        google_event_id="g3",
        end="2026-06-07T12:00:00+03:00",
        summary="🚫 Crypto Market Closed",
        start="2026-06-07T10:00:00+03:00",
        event_type="crypto_closure",
    )


def test_types_filter_includes_only_matching_events() -> None:
    state = make_state()
    state.posts["p1"].events.append(crypto_event())
    ics = render_ics(state, (), types=frozenset({"crypto_closure"}), now=NOW)
    assert ics.count("BEGIN:VEVENT") == 1
    assert "Crypto Market Closed" in ics
    assert "Platform Maintenance" not in ics


def test_no_types_filter_includes_everything() -> None:
    state = make_state()
    state.posts["p1"].events.append(crypto_event())
    ics = render_ics(state, (), now=NOW)
    assert ics.count("BEGIN:VEVENT") == 2


def test_typeless_legacy_events_appear_only_unfiltered() -> None:
    state = make_state()
    state.posts["p1"].events[0].event_type = ""  # pre-v3 entry
    assert render_ics(state, (), now=NOW).count("BEGIN:VEVENT") == 1
    filtered = render_ics(state, (), types=frozenset({"maintenance"}), now=NOW)
    assert filtered.count("BEGIN:VEVENT") == 0


def test_filtered_calendar_is_named_after_filter() -> None:
    ics = render_ics(make_state(), (), types=frozenset({"maintenance"}), now=NOW)
    assert "X-WR-CALNAME:FTMO Trading Updates (maintenance)" in ics


def _post(firm: str, key: str, url: str = "") -> PostState:
    return PostState(
        content_hash="h",
        last_seen="2026-06-09T00:00:00+00:00",
        firm=firm,
        url=url,
        events=[
            TrackedEvent(
                event_key=key,
                google_event_id=f"g-{key}",
                end="2026-06-10T14:00:00+03:00",
                summary=f"{firm} event",
                start="2026-06-10T08:00:00+03:00",
                event_type="maintenance",
            )
        ],
    )


def _descriptions(ics: str) -> dict[str, str]:
    """UID key -> DESCRIPTION line of that VEVENT."""
    found: dict[str, str] = {}
    for block in ics.split("BEGIN:VEVENT")[1:]:
        lines = block.split("\r\n")
        uid = next(line for line in lines if line.startswith("UID:"))[4:].split("@")[0]
        found[uid] = next((line for line in lines if line.startswith("DESCRIPTION:Source")), "")
    return found


def test_each_event_links_to_its_own_firms_announcement() -> None:
    """The combined feed linked every event to FTMO's page, Topstep's included."""
    state = State(
        posts={
            "a": _post("ftmo", "k-ftmo", "https://ftmo.com/en/blog/trading-updates/x/"),
            "b": _post("topstep", "k-top", "https://help.topstep.com/en/articles/1-holiday"),
        }
    )
    ics = render_ics(state, (), source_url="https://ftmo.com/en/trading-updates/", now=NOW)
    links = _descriptions(ics)
    assert "https://ftmo.com/en/blog/trading-updates/x/" in links["k-ftmo"]
    assert "https://help.topstep.com/en/articles/1-holiday" in links["k-top"]
    assert "ftmo.com" not in links["k-top"]


def test_a_pre_v5_post_without_a_url_falls_back_to_its_firms_page() -> None:
    state = State(posts={"a": _post("e8-markets", "k-e8"), "b": _post("", "k-legacy")})
    ics = render_ics(
        state,
        (),
        source_url="https://ftmo.com/en/trading-updates/",
        default_firm="ftmo",
        firm_urls={
            "e8-markets": "https://help.e8markets.com/en/articles/12122593",
            "ftmo": "https://ftmo.com/en/trading-updates/",
        },
        now=NOW,
    )
    links = _descriptions(ics)
    assert "help.e8markets.com" in links["k-e8"]
    # Unattributed state belongs to the first configured firm (State.firm_of).
    assert "https://ftmo.com/en/trading-updates/" in links["k-legacy"]
