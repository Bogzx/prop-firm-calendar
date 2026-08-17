"""Hosted mode: periodic sync loop + HTTP server exposing the ICS feed.

One person runs `ftmo-calendar serve` (or the Docker container); any trader
subscribes to `http://host:port/feed.ics` from Google/Apple/Outlook calendar —
no OAuth, no API keys on the subscriber side.

Endpoints: GET /feed.ics (the calendar), GET /status (HTML), GET /healthz (JSON).

`/healthz` is the contract monitors are pointed at (docs/DEPLOYMENT.md), so it
answers "is this feed trustworthy right now?", not merely "is the process up":
it returns 503 when the last sync errored, when no successful sync has landed
within twice the configured interval, or when the last run reported an anomaly
(see pipeline.RunReport.anomalies).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ftmo_calendar.state import load_state
from ftmo_calendar.stats import StatsStore

logger = logging.getLogger(__name__)

# A sync is considered overdue once this many intervals have passed without a
# successful run. Two gives one whole interval of slack for a slow or retried
# run before a monitor is told the feed has gone stale.
STALE_INTERVALS = 2

# Distinct ?types= combinations to keep rendered feeds for. Subscribers pick
# from seven checkboxes, so real traffic never approaches this; the cap only
# stops a crafted request loop from growing the cache without bound.
_MAX_CACHED_FILTERS = 64


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class FirmStatus:
    """Per-firm health, tracked separately so no firm can hide behind the others.

    A combined feed averages ten firms into one badge, and an averaged badge is
    exactly how a source that quietly stopped publishing stays unnoticed for
    weeks. Each firm therefore carries its own last-success stamp, error and
    anomalies, and is judged stale on the same rule the whole server is.
    """

    name: str
    display_name: str
    last_success: str | None = None
    last_error: str | None = None
    anomalies: tuple[str, ...] = ()
    runs_ok: int = 0
    runs_failed: int = 0

    def snapshot(self, now: datetime, stale_after: float, fallback: str | None) -> dict:
        since = _age(now, self.last_success or fallback)
        age = _age(now, self.last_success) if self.last_success else None
        stale = bool(stale_after and since is not None and since > stale_after)
        ok = self.last_error is None and not stale and not self.anomalies
        return {
            "firm": self.name,
            "display_name": self.display_name,
            "ok": ok,
            "status": "error"
            if self.last_error
            else "stale"
            if stale
            else "anomaly"
            if self.anomalies
            else "ok",
            "last_success": self.last_success,
            "last_success_age_seconds": None if age is None else round(age, 1),
            "stale": stale,
            "last_error": self.last_error,
            "anomalies": list(self.anomalies),
            "runs_ok": self.runs_ok,
            "runs_failed": self.runs_failed,
        }


@dataclass
class ServerStatus:
    """Thread-safe record of how the background sync is doing.

    `ok` is deliberately more than "no exception was raised": a sync that
    stopped running weeks ago, or one that ran but reported an anomaly, leaves
    subscribers with a silently frozen calendar. Both make `ok` false so the
    documented `/healthz` monitor and the status badge notice.

    With several firms configured it is also more than "the run finished": any
    single unhealthy firm makes the whole snapshot unhealthy, because the
    alternative is a green light over a feed that has silently lost a source.
    """

    started_at: str
    interval_seconds: float = 0
    source: str = "FTMO"
    last_run: str | None = None
    last_success: str | None = None
    last_error: str | None = None
    runs_ok: int = 0
    runs_failed: int = 0
    anomalies: tuple[str, ...] = ()
    firms: dict[str, FirmStatus] = field(default_factory=dict)
    clock: Callable[[], datetime] = _utcnow
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_success(
        self,
        now: datetime | None = None,
        anomalies: Sequence[str] | None = None,
        firms: Sequence[object] | None = None,
    ) -> None:
        with self._lock:
            stamp = (now or self.clock()).isoformat()
            self.last_run = stamp
            self.last_success = stamp
            self.last_error = None
            self.anomalies = tuple(anomalies or ())
            self.runs_ok += 1
            for outcome in firms or ():
                self._record_firm(outcome, stamp)

    def _record_firm(self, outcome: object, stamp: str) -> None:
        """Fold one firms.FirmOutcome into its FirmStatus (duck-typed to avoid a cycle)."""
        name = str(getattr(outcome, "name", "") or "")
        if not name:
            return
        entry = self.firms.get(name)
        if entry is None:
            entry = FirmStatus(name=name, display_name=str(getattr(outcome, "display_name", name)))
            self.firms[name] = entry
        entry.display_name = str(getattr(outcome, "display_name", name)) or name
        error = getattr(outcome, "error", None)
        entry.anomalies = tuple(getattr(outcome, "anomalies", ()) or ())
        entry.last_error = str(error) if error else None
        if error:
            entry.runs_failed += 1
        else:
            # Only a firm that actually completed gets its freshness stamp
            # renewed; otherwise a failing firm rides on the loop's success.
            entry.last_success = stamp
            entry.runs_ok += 1

    def record_failure(self, error: BaseException, now: datetime | None = None) -> None:
        with self._lock:
            self.last_run = (now or self.clock()).isoformat()
            self.last_error = str(error)
            self.runs_failed += 1

    @property
    def stale_after_seconds(self) -> float:
        return self.interval_seconds * STALE_INTERVALS

    def snapshot(self, now: datetime | None = None) -> dict:
        with self._lock:
            now = now or self.clock()
            next_run = None
            if self.last_run and self.interval_seconds:
                next_dt = datetime.fromisoformat(self.last_run) + timedelta(
                    seconds=self.interval_seconds
                )
                next_run = next_dt.isoformat()

            # Staleness is measured from the last *successful* run: a loop that
            # keeps failing on schedule must not look fresh just because it is
            # busy. Before the first success, a just-started process is given
            # the same grace window from startup, so a restart does not report
            # unhealthy the moment it comes up.
            since = _age(now, self.last_success or self.started_at)
            # Reported separately, and None until a success actually happens —
            # the grace window is not something to call a "last success age".
            age = _age(now, self.last_success) if self.last_success else None

            stale = bool(
                self.interval_seconds and since is not None and since > self.stale_after_seconds
            )
            sources = [
                f.snapshot(now, self.stale_after_seconds, self.started_at)
                for f in self.firms.values()
            ]
            unhealthy = [s["display_name"] for s in sources if not s["ok"]]
            ok = self.last_error is None and not stale and not self.anomalies and not unhealthy
            status = (
                "error"
                if self.last_error
                else "stale"
                if stale
                else "anomaly"
                if self.anomalies
                else "degraded"
                if unhealthy
                else "ok"
            )
            return {
                "ok": ok,
                "status": status,
                "source": self.source,
                "sources": sources,
                "unhealthy_sources": unhealthy,
                "started_at": self.started_at,
                "last_run": self.last_run,
                "last_success": self.last_success,
                "next_run": next_run,
                "last_success_age_seconds": None if age is None else round(age, 1),
                "stale": stale,
                "stale_after_seconds": self.stale_after_seconds or None,
                "last_error": self.last_error,
                "anomalies": list(self.anomalies),
                "runs_ok": self.runs_ok,
                "runs_failed": self.runs_failed,
            }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _age(now: datetime, stamp: str | None) -> float | None:
    """Seconds between an ISO timestamp and now; None if it cannot be read."""
    if not stamp:
        return None
    try:
        return (now - _aware(datetime.fromisoformat(stamp))).total_seconds()
    except ValueError:  # pragma: no cover - stamps are written by this class
        return None


#: What a sync may hand back: the original anomaly sequence, or any object
#: exposing `.anomalies` and `.outcomes` (firms.MultiRunReport does both).
SyncResult = Sequence[str] | object | None


def run_sync_loop(
    sync_fn: Callable[[], SyncResult],
    interval_seconds: float,
    stop: threading.Event,
    status: ServerStatus,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Run sync_fn immediately and then every interval until stop is set.

    A failing sync is recorded and reported but never kills the loop — the
    feed keeps serving the last good data. A persistent identical error is
    notified once, not every interval; a success resets the dedup so a
    recurring flap still alerts.

    `sync_fn` may return a sequence of anomaly strings: a run that completed
    without raising but produced a suspicious result (see
    pipeline.RunReport.anomalies). They are recorded on the status so
    `/healthz` and the status page stop reporting healthy.
    """
    last_notified_error: str | None = None
    while not stop.is_set():
        try:
            result = sync_fn()
            # A caller may hand back either a plain anomaly sequence (the
            # original contract, still honoured) or a run report carrying
            # per-firm outcomes. Duck-typed rather than imported, so server.py
            # keeps no dependency on the pipeline.
            firms = getattr(result, "outcomes", None)
            raw = getattr(result, "anomalies", result)
            anomalies = list(raw) if isinstance(raw, Sequence) else None
            status.record_success(anomalies=anomalies, firms=firms)
            last_notified_error = None
        except Exception as e:  # noqa: BLE001 - loop must survive any sync failure
            logger.exception("Scheduled sync failed")
            status.record_failure(e)
            if on_error is not None and str(e) != last_notified_error:
                last_notified_error = str(e)
                try:
                    on_error(e)
                except Exception:  # noqa: BLE001
                    logger.warning("Error notification failed", exc_info=True)
        if stop.wait(interval_seconds):
            break


@dataclass(frozen=True)
class FeedSelection:
    """What a `/feed.ics` request asked for. `None` on an axis means "no filter".

    Kept as one value rather than two parameters so the renderer signature, the
    cache key and the query parsing cannot drift apart — and so that adding a
    third filter later does not touch three call sites again.
    """

    types: frozenset[str] | None = None
    firms: frozenset[str] | None = None

    @property
    def unfiltered(self) -> bool:
        return self.types is None and self.firms is None


def make_handler(
    ics_path: Path,
    state_path: Path,
    status: ServerStatus,
    feed_renderer: Callable[[FeedSelection], bytes] | None = None,
    stats: StatsStore | None = None,
    valid_firms: Sequence[str] | None = None,
) -> type[BaseHTTPRequestHandler]:
    from ftmo_calendar.models import EventType

    valid_types = {t.value for t in EventType}
    known_firms = set(valid_firms or ())

    # Rendering a filtered feed re-reads the state file and regenerates the
    # whole calendar including the VTIMEZONE bisection. The unfiltered feed is
    # already served from a file on disk; give the filtered variants the same
    # treatment by caching per type-set, invalidated by the state file's mtime
    # and size so a completed sync is picked up on the next request.
    filtered_cache: dict[FeedSelection, tuple[tuple[float, int], bytes]] = {}
    cache_lock = threading.Lock()

    def _state_version() -> tuple[float, int]:
        try:
            stat = state_path.stat()
        except OSError:
            return (0.0, 0)
        return (stat.st_mtime, stat.st_size)

    def render_filtered(requested: FeedSelection) -> bytes:
        assert feed_renderer is not None
        version = _state_version()
        with cache_lock:
            cached = filtered_cache.get(requested)
            if cached is not None and cached[0] == version:
                return cached[1]
        body = feed_renderer(requested)
        with cache_lock:
            if len(filtered_cache) >= _MAX_CACHED_FILTERS:
                filtered_cache.clear()
            filtered_cache[requested] = (version, body)
        return body

    class Handler(BaseHTTPRequestHandler):
        server_version = "ftmo-calendar"  # don't advertise the Python version
        sys_version = ""

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
            logger.debug("http: " + format, *args)

        def _respond(
            self,
            code: int,
            content_type: str,
            body: bytes,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in extra_headers or []:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _cookie(self, name: str) -> str:
            jar = SimpleCookie()
            jar.load(self.headers.get("Cookie", ""))
            morsel = jar.get(name)
            return morsel.value if morsel else ""

        def _client_hash(self) -> str:
            raw = f"{self.client_address[0]}|{self.headers.get('User-Agent', '')}"
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

        def _json(self, code: int, payload: dict) -> None:
            self._respond(code, "application/json; charset=utf-8", json.dumps(payload).encode())

        def _parse_filter(
            self, raw: str, valid: set[str], label: str
        ) -> frozenset[str] | None | dict:
            """frozenset for a usable filter, None when absent, or an error payload."""
            if not raw:
                return None
            requested = frozenset(v.strip() for v in raw.split(",") if v.strip())
            unknown = requested - valid
            if unknown or not requested:
                return {
                    "error": f"unknown {label}: {sorted(unknown)}",
                    "valid": sorted(valid),
                }
            return requested

        def _serve_feed(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            types_param = query.get("types", [""])[0]
            firms_param = query.get("firms", [""])[0]
            if (types_param or firms_param) and feed_renderer is not None:
                types = self._parse_filter(types_param, valid_types, "types")
                if isinstance(types, dict):
                    self._json(400, types)
                    return
                firms = self._parse_filter(firms_param, known_firms, "firms")
                if isinstance(firms, dict):
                    self._json(400, firms)
                    return
                selection = FeedSelection(types=types, firms=firms)
                self._respond(200, "text/calendar; charset=utf-8", render_filtered(selection))
                return
            # No filter: serve the file on disk, exactly as before. This is the
            # URL real subscribers already have, and it must keep returning the
            # same bytes for the same state.
            if not ics_path.exists():
                self._json(404, {"error": "feed not generated yet"})
                return
            self._respond(200, "text/calendar; charset=utf-8", ics_path.read_bytes())

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                # 503 on unhealthy: a monitor pointed here (docs/DEPLOYMENT.md)
                # can only page you if the status code moves.
                payload = status.snapshot()
                self._json(200 if payload["ok"] else 503, payload)
            elif path == "/stats":
                if stats is None:
                    self._json(404, {"error": "stats not enabled"})
                else:
                    self._json(200, stats.snapshot())
            elif path == "/feed.ics":
                if stats is not None:
                    stats.record_feed_hit(self._client_hash())
                self._serve_feed()
            elif path in ("/", "/status"):
                from ftmo_calendar.web import render_page

                extra_headers: list[tuple[str, str]] = []
                stats_snapshot = None
                if stats is not None:
                    visitor_id = self._cookie("aftc_id")
                    if not visitor_id:
                        visitor_id = secrets.token_hex(8)
                        extra_headers.append(
                            (
                                "Set-Cookie",
                                f"aftc_id={visitor_id}; Max-Age=31536000; Path=/; "
                                "SameSite=Lax; HttpOnly",
                            )
                        )
                    stats.record_page_view(visitor_id)
                    stats_snapshot = stats.snapshot()
                body = render_page(load_state(state_path), status.snapshot(), stats_snapshot)
                self._respond(200, "text/html; charset=utf-8", body, extra_headers)
            else:
                self._json(404, {"error": "not found"})

    return Handler


class DataDirError(Exception):
    """The data directory cannot be written to — nothing would ever persist."""


def check_writable(directory: Path) -> None:
    """Fail fast when state/feed writes would silently vanish.

    The container runs as uid 1000 against a bind mount. If ./data belongs to
    another uid, every write fails, but the process stays up, the healthcheck
    passes, and the feed quietly never updates — exactly the silent failure
    this tool exists to prevent.
    """
    probe = directory / ".ftmo-calendar-write-test"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise DataDirError(
            f"data directory {directory} is not writable ({e}). "
            "State, stats and the ICS feed could never be saved. In Docker the "
            "container runs as uid 1000: run `chown -R 1000:1000 data` on the host."
        ) from e


def serve_forever(
    host: str,
    port: int,
    interval_seconds: float,
    ics_path: Path,
    state_path: Path,
    sync_fn: Callable[[], SyncResult],
    on_error: Callable[[BaseException], None] | None = None,
    feed_renderer: Callable[[FeedSelection], bytes] | None = None,
    stats: StatsStore | None = None,
    source_name: str = "FTMO",
    valid_firms: Sequence[str] | None = None,
) -> int:
    check_writable(state_path.parent)
    status = ServerStatus(
        started_at=datetime.now(UTC).isoformat(),
        interval_seconds=interval_seconds,
        source=source_name,
    )
    stop = threading.Event()
    loop_thread = threading.Thread(
        target=run_sync_loop,
        args=(sync_fn, interval_seconds, stop, status, on_error),
        daemon=True,
        name="sync-loop",
    )
    loop_thread.start()
    httpd = ThreadingHTTPServer(
        (host, port),
        make_handler(ics_path, state_path, status, feed_renderer, stats, valid_firms),
    )
    logger.info(
        "Serving on http://%s:%d (feed: /feed.ics, status: /status); sync every %.0f min",
        host,
        port,
        interval_seconds / 60,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        stop.set()
        httpd.server_close()
        if stats is not None:
            stats.flush()  # writes are debounced; don't lose the last window
    return 0
