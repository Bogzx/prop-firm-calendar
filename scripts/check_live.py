#!/usr/bin/env python3
"""Check a running prop-firm-calendar instance from the outside, as a subscriber would.

    python scripts/check_live.py https://calendar.bogdantruta.com --report report.md

`/healthz` already turns 503 when the sync is broken, stale or suspicious, but
only if something is polling it — and it cannot report its own host being
down, its certificate expiring or the feed it serves being unreadable. This
script is that something: it is run on a schedule by
.github/workflows/live-monitor.yml, which opens an issue when it fails and
closes it when it recovers.

Checks:
  healthz   200 and `ok` (the server's own verdict: fresh, no error, no anomaly,
            no unhealthy firm)
  feed      /feed.ics is a well-formed calendar a client can subscribe to:
            CRLF, folded at 75 octets on character boundaries, balanced
            components, UID/DTSTAMP/DTSTART on every event, unique UIDs, every
            TZID defined, and at least --min-events events. Parsed by
            `icalendar` too when that package is installed.
  api       /api/v1/next answers JSON with a row per configured firm

Exit 0 when everything passes, 1 otherwise. Standard library only (icalendar
optional). Requests carry MONITOR_USER_AGENT, which the server leaves out of
its usage statistics.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MONITOR_USER_AGENT = "prop-firm-calendar-monitor/1.0 (+https://github.com/Bogzx/prop-firm-calendar)"
FOLD_OCTETS = 75


@dataclass(frozen=True)
class Result:
    name: str
    #: "ok", or a short stable code for what is wrong ("stale", "http-502",
    #: "unreachable", …). Codes, not details, decide whether an open alert
    #: needs a new comment, so ages and timestamps must stay out of them.
    code: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.code == "ok"


def fetch(url: str, timeout: float) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": MONITOR_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers.get("Content-Type", ""), e.read()


def _get(name: str, url: str, timeout: float) -> tuple[Result | None, int, str, bytes]:
    try:
        status, ctype, body = fetch(url, timeout)
    except http.client.HTTPException as e:
        # Connected, but the answer was cut short or malformed (IncompleteRead,
        # a bad status line): a proxy or a dying process, not a network blip.
        detail = f"{url}: {type(e).__name__}: {e}"
        return Result(name, "bad-response", detail), 0, "", b""
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return Result(name, "unreachable", f"{url}: {reason}"), 0, "", b""
    return None, status, ctype, body


def check_health(base: str, timeout: float) -> Result:
    failed, status, _, body = _get("healthz", f"{base}/healthz", timeout)
    if failed:
        return failed
    try:
        payload = json.loads(body)
    except ValueError:
        return Result("healthz", f"http-{status}", f"HTTP {status}, not JSON: {body[:120]!r}")
    if not isinstance(payload, dict):
        return Result(
            "healthz", "not-json-object", f"HTTP {status}, not a JSON object: {body[:120]!r}"
        )
    if status == 200 and payload.get("ok") is True:
        age = payload.get("last_success_age_seconds")
        when = f"{age / 3600:.1f} h ago" if isinstance(age, (int, float)) else "unknown"
        return Result("healthz", "ok", f"last successful sync {when}")
    verdict = str(payload.get("status") or f"http-{status}")
    parts = [f"HTTP {status}, status {verdict!r}"]
    unhealthy = payload.get("unhealthy_sources")
    if isinstance(unhealthy, list) and unhealthy:
        parts.append("unhealthy: " + ", ".join(map(str, unhealthy)))
    if payload.get("last_error"):
        parts.append(f"last error: {str(payload['last_error'])[:300]}")
    anomalies = payload.get("anomalies")
    for anomaly in anomalies[:3] if isinstance(anomalies, list) else ():
        parts.append(f"anomaly: {str(anomaly)[:300]}")
    return Result("healthz", verdict, "; ".join(parts))


def ics_problems(raw: bytes, min_events: int = 1) -> list[tuple[str, str]]:
    """(code, detail) for every way `raw` falls short of a subscribable calendar."""
    problems: list[tuple[str, str]] = []
    if not raw.endswith(b"\r\n"):
        problems.append(("line-endings", "does not end with CRLF"))
    stripped = raw.replace(b"\r\n", b"")
    if b"\n" in stripped or b"\r" in stripped:
        problems.append(("line-endings", "bare LF or CR outside a CRLF"))
    physical = raw.split(b"\r\n")
    longest = max((len(line) for line in physical), default=0)
    if longest > FOLD_OCTETS:
        problems.append(("folding", f"a line of {longest} octets (limit {FOLD_OCTETS})"))
    if any(line[:1] == b" " and len(line) > 1 and line[1] & 0xC0 == 0x80 for line in physical):
        problems.append(("folding", "a fold splits a UTF-8 character"))

    # Unfold before decoding: a fold inside a multi-byte character (reported
    # above) leaves each physical line undecodable on its own.
    try:
        text = raw.replace(b"\r\n ", b"").decode("utf-8")
    except UnicodeDecodeError as e:
        return [*problems, ("not-utf8", str(e))]
    lines = [line for line in text.split("\r\n") if line]
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        problems.append(("structure", "not wrapped in BEGIN:VCALENDAR … END:VCALENDAR"))
        return problems

    stack: list[str] = []
    events: list[dict[str, list[str]]] = []
    calendar_props: set[str] = set()
    timezones: set[str] = set()
    referenced: set[str] = set()
    for line in lines:
        name, _, value = line.partition(":")
        prop, *params = name.split(";")
        if prop == "BEGIN":
            stack.append(value)
            if value == "VEVENT":
                events.append({})
            continue
        if prop == "END":
            if not stack or stack.pop() != value:
                problems.append(("structure", f"END:{value} does not close what is open"))
                return problems
            continue
        if not stack:
            problems.append(("structure", f"property outside any component: {line[:60]}"))
            continue
        current = stack[-1]
        if current == "VCALENDAR":
            calendar_props.add(prop)
        elif current == "VTIMEZONE" and prop == "TZID":
            timezones.add(value)
        elif current == "VEVENT":
            events[-1].setdefault(prop, []).append(value)
            referenced.update(p.split("=", 1)[1] for p in params if p.startswith("TZID="))
    if stack:
        problems.append(("structure", f"unclosed component(s): {', '.join(stack)}"))
    for required in ("VERSION", "PRODID"):
        if required not in calendar_props:
            problems.append(("structure", f"calendar has no {required}"))
    for index, event in enumerate(events, 1):
        for required in ("UID", "DTSTAMP", "DTSTART"):
            if len(event.get(required, [])) != 1:
                problems.append(("event", f"event {index} needs exactly one {required}"))
    uids = [event["UID"][0] for event in events if event.get("UID")]
    if len(set(uids)) != len(uids):
        problems.append(("event", "duplicate UIDs"))
    if referenced - timezones:
        problems.append(
            ("timezone", f"TZID not defined: {', '.join(sorted(referenced - timezones))}")
        )
    if len(events) < min_events:
        problems.append(("no-events", f"{len(events)} event(s), expected at least {min_events}"))
    return problems


def _icalendar_problem(raw: bytes) -> str | None:
    try:
        import icalendar
    except ImportError:
        return None
    try:
        calendar = icalendar.Calendar.from_ical(raw)
        for event in calendar.walk("VEVENT"):
            event.decoded("DTSTART")  # resolves the TZID against the feed's VTIMEZONE
    except Exception as e:  # noqa: BLE001 - any parser failure is the finding
        return f"icalendar could not read it: {e}"
    return None


def check_feed(base: str, timeout: float, min_events: int) -> Result:
    failed, status, ctype, body = _get("feed", f"{base}/feed.ics", timeout)
    if failed:
        return failed
    if status != 200:
        return Result("feed", f"http-{status}", f"HTTP {status}")
    if "text/calendar" not in ctype:
        return Result("feed", "content-type", f"served as {ctype or 'nothing'}, not text/calendar")
    problems = ics_problems(body, min_events)
    parser = _icalendar_problem(body)
    if parser:
        problems.append(("unparseable", parser))
    if problems:
        return Result("feed", problems[0][0], "; ".join(detail for _, detail in problems))
    count = body.count(b"BEGIN:VEVENT")
    return Result("feed", "ok", f"{count} events, well-formed ({len(body)} bytes)")


def check_api(base: str, timeout: float) -> Result:
    failed, status, _, body = _get("api", f"{base}/api/v1/next", timeout)
    if failed:
        return failed
    if status != 200:
        return Result("api", f"http-{status}", f"/api/v1/next answered HTTP {status}")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return Result("api", "not-json", f"/api/v1/next is not a JSON object: {body[:120]!r}")
    firms = payload.get("firms")
    rows = isinstance(firms, list) and firms
    if not rows or not all(isinstance(f, dict) and isinstance(f.get("firm"), str) for f in firms):
        return Result("api", "shape", "/api/v1/next has no per-firm rows")
    return Result("api", "ok", f"{len(firms)} firm(s): {', '.join(f['firm'] for f in firms)}")


def signature(results: list[Result]) -> str:
    return " ".join(f"{r.name}={r.code}" for r in results)


def report(base: str, results: list[Result]) -> str:
    verdict = "PASS" if all(r.ok for r in results) else "FAIL"
    lines = [
        f"## Live check: {verdict} — {base}",
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for r in results:
        detail = r.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r.name} | {'ok' if r.ok else r.code} | {detail} |")
    lines += ["", f"<!-- signature: {signature(results)} -->"]
    return "\n".join(lines) + "\n"


def _guarded(name: str, check: Callable[[], Result]) -> Result:
    """A check that crashes is a failed check, never a missing report.

    The workflow reads the report to open the alert issue; an exception here
    would leave none, and the outage would go unreported.
    """
    try:
        return check()
    except Exception as e:  # noqa: BLE001 - any crash is the finding
        return Result(name, "crashed", f"the check itself failed: {type(e).__name__}: {e}")


def run(base: str, *, timeout: float = 20.0, min_events: int = 1) -> list[Result]:
    base = base.rstrip("/")
    return [
        _guarded("healthz", lambda: check_health(base, timeout)),
        _guarded("feed", lambda: check_feed(base, timeout, min_events)),
        _guarded("api", lambda: check_api(base, timeout)),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base_url", help="e.g. https://calendar.bogdantruta.com")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds per request")
    parser.add_argument(
        "--min-events",
        type=int,
        default=1,
        help="fewest events the unfiltered feed may hold (it keeps ~45 days of history)",
    )
    parser.add_argument("--report", type=Path, help="also write a Markdown report here")
    args = parser.parse_args(argv)

    results = run(args.base_url, timeout=args.timeout, min_events=args.min_events)
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'} {r.name}: {r.detail}")
    if args.report:
        args.report.write_text(report(args.base_url.rstrip("/"), results), encoding="utf-8")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
