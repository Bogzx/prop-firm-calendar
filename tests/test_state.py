import json
from datetime import UTC, datetime
from pathlib import Path

from ftmo_calendar.state import PostState, State, TrackedEvent, load_state, save_state

NOW = datetime(2026, 6, 9, tzinfo=UTC)


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    state = load_state(tmp_path / "state.json")
    assert state.posts == {}
    assert state.last_heartbeat is None


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State(
        posts={
            "p1": PostState(
                content_hash="abc",
                last_seen="2026-06-09T00:00:00+00:00",
                url="https://example.com/post",
                events=[
                    TrackedEvent(
                        "k1",
                        "gid1",
                        "2026-06-10T00:00:00+00:00",
                        summary="⚠️ Maintenance",
                        start="2026-06-09T22:00:00+00:00",
                    )
                ],
            )
        },
        last_heartbeat="2026-06-09T06:00:00+00:00",
    )
    save_state(state, path)
    loaded = load_state(path)
    assert loaded.posts["p1"].content_hash == "abc"
    assert loaded.posts["p1"].url == "https://example.com/post"
    assert loaded.posts["p1"].events[0].google_event_id == "gid1"
    assert loaded.posts["p1"].events[0].summary == "⚠️ Maintenance"
    assert loaded.posts["p1"].events[0].start == "2026-06-09T22:00:00+00:00"
    assert loaded.last_heartbeat == "2026-06-09T06:00:00+00:00"


def test_v1_state_file_loads_with_defaults(tmp_path: Path) -> None:
    """Phase 1 state files lack summary/start/last_heartbeat — must still load."""
    path = tmp_path / "state.json"
    v1 = {
        "version": 1,
        "posts": {
            "p1": {
                "content_hash": "abc",
                "last_seen": "2026-06-09T00:00:00+00:00",
                "events": [
                    {
                        "event_key": "k1",
                        "google_event_id": "gid1",
                        "end": "2026-06-10T00:00:00+00:00",
                    }
                ],
            }
        },
    }
    path.write_text(json.dumps(v1), encoding="utf-8")
    state = load_state(path)
    event = state.posts["p1"].events[0]
    assert event.summary == "" and event.start == ""
    assert state.last_heartbeat is None


def test_prune_drops_stale_posts_with_ended_events(tmp_path: Path) -> None:
    state = State(
        posts={
            "stale": PostState(
                content_hash="a",
                last_seen="2026-01-01T00:00:00+00:00",
                events=[TrackedEvent("k", "g", "2026-01-02T00:00:00+00:00")],
            ),
            "stale-but-future-event": PostState(
                content_hash="b",
                last_seen="2026-01-01T00:00:00+00:00",
                events=[TrackedEvent("k2", "g2", "2026-07-01T00:00:00+00:00")],
            ),
            "fresh": PostState(content_hash="c", last_seen="2026-06-08T00:00:00+00:00"),
        }
    )
    state.prune(now=NOW)
    assert set(state.posts) == {"stale-but-future-event", "fresh"}


def test_corrupt_state_file_resets(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    state = load_state(path)
    assert state.posts == {}
