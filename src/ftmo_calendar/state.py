"""Persistent run state: which posts were seen and which events were created."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_VERSION = 4
_PRUNE_AFTER_DAYS = 45


@dataclass
class TrackedEvent:
    event_key: str
    google_event_id: str
    end: str  # ISO 8601, timezone-aware
    summary: str = ""  # display data for ICS export (v2)
    start: str = ""  # ISO 8601, timezone-aware (v2)
    event_type: str = ""  # EventType value, used for filtered feeds (v3)


@dataclass
class PostState:
    content_hash: str
    last_seen: str  # ISO 8601, timezone-aware
    events: list[TrackedEvent] = field(default_factory=list)
    #: Profile name of the firm this post came from (v4). Empty on entries
    #: written before multi-firm support; those were necessarily produced by
    #: the single configured source, so readers attribute them to the first
    #: configured firm rather than dropping them from filtered feeds. Deliberately
    #: *not* part of TradingEvent.event_key — adding it would recompute every
    #: existing key and orphan every event already in subscribers' calendars.
    firm: str = ""


@dataclass
class State:
    posts: dict[str, PostState] = field(default_factory=dict)
    last_heartbeat: str | None = None  # ISO 8601 of the last heartbeat notification

    def firm_of(self, post: PostState, default_firm: str = "") -> str:
        """The firm a post belongs to, attributing pre-v4 entries to `default_firm`.

        Callers pass the first configured firm. Any state written before
        multi-firm support came from the one source that was configured, and
        that source is what the first `[[firms]]` entry (or the derived
        `[source]` firm) now is — so the attribution is not a guess. Without
        it, an upgraded deployment's existing events would silently vanish from
        every per-firm feed until each post happened to be re-scraped.
        """
        return post.firm or default_firm

    def firms(self, default_firm: str = "") -> list[str]:
        """Distinct firm names present in the state, sorted."""
        return sorted({self.firm_of(p, default_firm) for p in self.posts.values()} - {""})

    def prune(self, now: datetime | None = None) -> None:
        """Drop posts not seen recently whose events have all ended."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=_PRUNE_AFTER_DAYS)
        for key in list(self.posts):
            post = self.posts[key]
            last_seen_dt = datetime.fromisoformat(post.last_seen)
            if last_seen_dt.tzinfo is None:
                last_seen_dt = last_seen_dt.replace(tzinfo=UTC)
            if last_seen_dt >= cutoff:
                continue
            all_ended = True
            for e in post.events:
                end_dt = datetime.fromisoformat(e.end)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=UTC)
                if end_dt >= now:
                    all_ended = False
                    break
            if all_ended:
                del self.posts[key]


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        posts = {
            key: PostState(
                content_hash=p["content_hash"],
                last_seen=p["last_seen"],
                firm=p.get("firm", ""),
                events=[
                    TrackedEvent(
                        event_key=e["event_key"],
                        google_event_id=e["google_event_id"],
                        end=e["end"],
                        summary=e.get("summary", ""),
                        start=e.get("start", ""),
                        event_type=e.get("event_type", ""),
                    )
                    for e in p.get("events", [])
                ],
            )
            for key, p in data.get("posts", {}).items()
        }
        return State(posts=posts, last_heartbeat=data.get("last_heartbeat"))
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("State file %s is corrupt (%s); starting fresh", path, e)
        return State()


def save_state(state: State, path: Path) -> None:
    payload = {
        "version": STATE_VERSION,
        "last_heartbeat": state.last_heartbeat,
        "posts": {k: asdict(v) for k, v in state.posts.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
