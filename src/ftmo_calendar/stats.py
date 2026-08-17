"""Simple, self-hosted, anonymous usage statistics.

Counts page views, unique visitors (random first-party cookie id), feed pulls,
and unique feed clients (hash of address + user agent). No third parties, no
personal data: visitor ids are random tokens, client hashes are one-way.
Persisted to stats.json next to the state file; daily history kept 30 days.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_HISTORY_DAYS = 30
_MAX_TRACKED_IDS = 10_000  # per day; cap memory on pathological traffic
_FLUSH_SECONDS = 60.0  # at most one disk write per minute (see _maybe_save)


class StatsStore:
    def __init__(self, path: Path, flush_seconds: float = _FLUSH_SECONDS) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._days: dict[str, dict[str, int]] = {}
        self._today = ""
        self._seen_visitors: set[str] = set()
        self._seen_clients: set[str] = set()
        self._flush_seconds = flush_seconds
        self._dirty = False
        self._last_flush = 0.0
        self._load()

    def record_page_view(self, visitor_id: str, now: datetime | None = None) -> None:
        with self._lock:
            day = self._roll(now)
            day["views"] += 1
            if (
                visitor_id not in self._seen_visitors
                and len(self._seen_visitors) < _MAX_TRACKED_IDS
            ):
                self._seen_visitors.add(visitor_id)
                day["visitors"] += 1
            self._maybe_save()

    def record_feed_hit(self, client_hash: str, now: datetime | None = None) -> None:
        with self._lock:
            day = self._roll(now)
            day["feed_hits"] += 1
            if client_hash not in self._seen_clients and len(self._seen_clients) < _MAX_TRACKED_IDS:
                self._seen_clients.add(client_hash)
                day["feed_clients"] += 1
            self._maybe_save()

    def flush(self) -> None:
        """Persist immediately if anything is pending."""
        with self._lock:
            if self._dirty:
                self._save()

    def snapshot(self, now: datetime | None = None) -> dict:
        with self._lock:
            self._roll(now)
            # Reading is rare (the status page and /stats) and is the natural
            # moment to make the on-disk copy current without paying a write
            # per request.
            if self._dirty:
                self._save()
            history = {key: dict(value) for key, value in self._days.items() if key != self._today}
            return {
                "today": dict(self._days[self._today]),
                "days": history,
                "totals": {
                    "views": sum(d["views"] for d in self._days.values()),
                    "feed_hits": sum(d["feed_hits"] for d in self._days.values()),
                },
            }

    def _roll(self, now: datetime | None) -> dict[str, int]:
        """Ensure today's bucket exists; archive and trim on day change."""
        today = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._seen_visitors = set()
            self._seen_clients = set()
            self._days.setdefault(
                today, {"views": 0, "visitors": 0, "feed_hits": 0, "feed_clients": 0}
            )
            for stale in sorted(self._days)[:-_HISTORY_DAYS]:
                del self._days[stale]
        return self._days[self._today]

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8-sig"))
            self._days = {
                key: {
                    "views": int(value.get("views", 0)),
                    "visitors": int(value.get("visitors", 0)),
                    "feed_hits": int(value.get("feed_hits", 0)),
                    "feed_clients": int(value.get("feed_clients", 0)),
                }
                for key, value in data.get("days", {}).items()
            }
            self._today = data.get("today", "")
            if self._today not in self._days:
                self._today = ""
            self._seen_visitors = set(data.get("seen_visitors", []))
            self._seen_clients = set(data.get("seen_clients", []))
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as e:
            logger.warning("Stats file %s is corrupt (%s); starting fresh", self._path, e)
            self._days = {}
            self._today = ""
            self._seen_visitors = set()
            self._seen_clients = set()

    def _maybe_save(self) -> None:
        """Debounced persist. Caller holds the lock.

        Serializing every counter and fsync-replacing the file on each HTTP
        request turned a request loop against the public feed into sustained
        disk I/O — a free amplification vector on a small VPS. Counters live in
        memory and reach disk at most once per flush window; the most that can
        be lost to a hard kill is one window of counts.
        """
        self._dirty = True
        elapsed = time.monotonic() - self._last_flush
        if elapsed >= self._flush_seconds:
            self._save()

    def _save(self) -> None:
        self._dirty = False
        self._last_flush = time.monotonic()
        payload = {
            "today": self._today,
            "days": self._days,
            "seen_visitors": sorted(self._seen_visitors),
            "seen_clients": sorted(self._seen_clients),
        }
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            # Stats are best-effort: a read-only or misowned data dir must
            # never take a page request down with it.
            logger.warning("Could not persist stats to %s: %s", self._path, e)
