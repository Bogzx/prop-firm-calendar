"""scripts/check_live.py: the outside-in check behind the live-monitor workflow.

Driven against a real `serve` handler on localhost, so what it calls healthy
or broken is what the deployed server actually answers.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from prop_firm_calendar.server import MONITOR_AGENT, ServerStatus, make_handler
from prop_firm_calendar.sinks.ics import render_ics, write_ics
from prop_firm_calendar.state import PostState, State, TrackedEvent, save_state
from prop_firm_calendar.stats import StatsStore

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_live", ROOT / "scripts" / "check_live.py")
assert _spec is not None and _spec.loader is not None
check_live = importlib.util.module_from_spec(_spec)
sys.modules["check_live"] = check_live  # dataclasses resolve annotations through it
_spec.loader.exec_module(check_live)

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


def make_state(events: int = 2) -> State:
    tracked = [
        TrackedEvent(
            event_key=f"key{i}",
            google_event_id=f"g{i}",
            start=f"2026-06-0{i + 5}T08:00:00+03:00",
            end=f"2026-06-0{i + 5}T14:00:00+03:00",
            summary=f"⚠️ Platform Maintenance — MT4, MT5, cTrader, TradingView, DXtrade {i}",
            event_type="maintenance",
        )
        for i in range(events)
    ]
    return State(posts={"p": PostState("h", NOW.isoformat(), tracked, firm="ftmo")})


def feed(events: int = 2) -> bytes:
    return render_ics(make_state(events), (60,), tz_name="Etc/GMT-3", now=NOW).encode()


# -- the feed checks ---------------------------------------------------------


def test_a_feed_the_writer_produces_has_no_problems() -> None:
    assert check_live.ics_problems(feed()) == []


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda raw: raw.replace(b"\r\n", b"\n"), "line-endings"),
        (lambda raw: raw.replace(b"\r\n ", b""), "folding"),  # unfolded: lines > 75 octets
        (lambda raw: raw.replace(b"END:VCALENDAR\r\n", b""), "structure"),
        (lambda raw: raw.replace(b"END:VTIMEZONE", b"END:VEVENT", 1), "structure"),
        (lambda raw: raw.replace(b"UID:key1@", b"UID:key0@"), "event"),
        (lambda raw: raw.replace(b"DTSTAMP:", b"X-DTSTAMP:", 1), "event"),
        (lambda raw: raw.replace(b"TZID:Etc/GMT-3", b"TZID:Etc/GMT-2"), "timezone"),
        (lambda raw: raw.replace(b"VERSION:2.0\r\n", b""), "structure"),
        (lambda raw: b"\xff" + raw, "not-utf8"),
    ],
)
def test_each_kind_of_broken_feed_is_named(mutate, code: str) -> None:
    codes = [c for c, _ in check_live.ics_problems(mutate(feed()))]
    assert code in codes


def test_a_fold_inside_a_character_is_caught() -> None:
    raw = feed()
    # Move one fold a byte to the left so it lands inside the em dash (3 octets).
    folded = raw.index("—".encode()) + 1
    broken = raw[:folded] + b"\r\n " + raw[folded:]
    assert ("folding", "a fold splits a UTF-8 character") in check_live.ics_problems(broken)


def test_an_empty_calendar_fails_the_minimum() -> None:
    assert [c for c, _ in check_live.ics_problems(feed(events=0))] == ["no-events"]
    assert check_live.ics_problems(feed(events=0), min_events=0) == []


# -- against a running server ------------------------------------------------


@pytest.fixture
def instance(tmp_path: Path) -> Iterator[tuple[str, ServerStatus, StatsStore, Path]]:
    state_path, ics_path = tmp_path / "state.json", tmp_path / "feed.ics"
    save_state(make_state(), state_path)
    write_ics(make_state(), ics_path, (60,), tz_name="Etc/GMT-3", now=NOW)
    status = ServerStatus(started_at=NOW.isoformat(), interval_seconds=3600, clock=lambda: NOW)
    stats = StatsStore(tmp_path / "stats.json")
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(ics_path, state_path, status, stats=stats, valid_firms=["ftmo"]),
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", status, stats, ics_path
    httpd.shutdown()
    httpd.server_close()


def test_a_healthy_instance_passes(instance, tmp_path: Path) -> None:
    base, status, _, _ = instance
    status.record_success(now=NOW)
    report = tmp_path / "report.md"
    assert check_live.main([base + "/", "--report", str(report)]) == 0
    text = report.read_text(encoding="utf-8")
    assert text.startswith(f"## Live check: PASS — {base}\n")
    assert "<!-- signature: healthz=ok feed=ok api=ok -->" in text


def test_a_failing_sync_fails_with_the_servers_reason(instance) -> None:
    base, status, _, _ = instance
    status.record_failure(RuntimeError("FTMO page structure changed"), now=NOW)
    health, feed_result, api = check_live.run(base)
    assert (health.code, feed_result.code, api.code) == ("error", "ok", "ok")
    assert "HTTP 503" in health.detail and "FTMO page structure changed" in health.detail


def test_an_anomaly_and_an_empty_feed_are_both_reported(instance) -> None:
    base, status, _, ics_path = instance
    status.record_success(now=NOW, anomalies=["E8 Markets: 2 extracted event(s) rejected"])
    write_ics(State(), ics_path, (60,), now=NOW)
    results = check_live.run(base)
    assert check_live.signature(results) == "healthz=anomaly feed=no-events api=ok"
    assert "E8 Markets" in results[0].detail


def test_signatures_ignore_volatile_details(instance) -> None:
    """An open alert is re-commented only when *what* is wrong changes, not its age."""
    base, status, _, _ = instance
    status.record_failure(RuntimeError("timeout after 30 s"), now=NOW)
    first = check_live.signature(check_live.run(base))
    status.record_failure(RuntimeError("timeout after 31 s"), now=NOW)
    assert check_live.signature(check_live.run(base)) == first


def test_the_monitor_is_not_counted_as_a_subscriber(instance) -> None:
    base, status, stats, _ = instance
    status.record_success(now=NOW)
    assert MONITOR_AGENT in check_live.MONITOR_USER_AGENT  # the two must not drift apart
    check_live.run(base)
    assert stats.snapshot()["today"]["feed_hits"] == 0
    request = urllib.request.Request(f"{base}/feed.ics", headers={"User-Agent": "Google-Calendar"})
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()
    assert stats.snapshot()["today"]["feed_hits"] == 1
