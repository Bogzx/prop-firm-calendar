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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


# -- answers that are not answers --------------------------------------------


class Canned(BaseHTTPRequestHandler):
    """Serves `self.server.replies[path]`: (status, body, declared length or None)."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        status, body, length = self.server.replies[self.path]  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True  # a short body then EOF: IncompleteRead

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def canned() -> Iterator[tuple[str, dict]]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Canned)
    httpd.replies = {}  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd.replies  # type: ignore[attr-defined]
    httpd.shutdown()
    httpd.server_close()


def test_a_truncated_answer_is_a_failed_check_with_a_report(canned, tmp_path: Path) -> None:
    """An IncompleteRead used to escape as a traceback: no report, so no issue."""
    base, replies = canned
    for path in ("/healthz", "/feed.ics", "/api/v1/next"):
        replies[path] = (200, b'{"ok": tr', 1000)
    report = tmp_path / "report.md"
    assert check_live.main([base, "--report", str(report)]) == 1
    text = report.read_text(encoding="utf-8")
    assert "healthz=bad-response feed=bad-response api=bad-response" in text
    assert "IncompleteRead" in text


@pytest.mark.parametrize("body", [b"[]", b'"ok"', b"null", b"42"])
def test_healthz_json_that_is_not_an_object_is_a_failure(canned, body: bytes) -> None:
    base, replies = canned
    replies["/healthz"] = (200, body, None)
    assert check_live.check_health(base, 5).code == "not-json-object"


@pytest.mark.parametrize(
    "body",
    [b"[]", b'{"firms": ["ftmo"]}', b'{"firms": [{"firm": 1}]}', b'{"firms": "ftmo"}'],
)
def test_api_answers_of_the_wrong_shape_are_failures(canned, body: bytes) -> None:
    base, replies = canned
    replies["/api/v1/next"] = (200, body, None)
    assert check_live.check_api(base, 5).code in {"not-json", "shape"}


def test_a_crashing_check_still_leaves_a_report(
    instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, status, _, _ = instance
    status.record_success(now=NOW)

    def explode(*args: object) -> None:
        raise KeyError("surprise")

    monkeypatch.setattr(check_live, "check_feed", explode)
    report = tmp_path / "report.md"
    assert check_live.main([base, "--report", str(report)]) == 1
    text = report.read_text(encoding="utf-8")
    assert "healthz=ok feed=crashed api=ok" in text and "KeyError" in text
