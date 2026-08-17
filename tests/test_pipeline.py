import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from ftmo_calendar.config import AppConfig, CalendarConfig, EventRules, LLMConfig, SourceConfig
from ftmo_calendar.models import SourcePost, TradingEvent
from ftmo_calendar.parsing.llm import RawEvent
from ftmo_calendar.pipeline import run_pipeline
from ftmo_calendar.state import PostState, State, TrackedEvent

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

POST = SourcePost(
    post_key="trading-update-2026-06-04",
    title="Trading Update | Jun 4 2026",
    text="ctrader maintenance on Saturday 6 Jun 2026 08:00 to 14:00 GMT+3",
    url="https://ftmo.com/en/trading-updates/",
)

RAW = RawEvent(
    event_type="maintenance",
    start_time="2026-06-06T08:00:00",
    end_time="2026-06-06T14:00:00",
    stated_utc_offset="+03:00",
)


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        source=SourceConfig(),
        llm=LLMConfig(api_key="test"),
        calendar=CalendarConfig(),
        events=EventRules(),
        base_dir=tmp_path,
    )


class FakeSource:
    def __init__(self, posts: list[SourcePost]) -> None:
        self.posts = posts

    def fetch(self) -> list[SourcePost]:
        return self.posts


class FakeExtractor:
    def __init__(self, result: list[RawEvent]) -> None:
        self.result = result
        self.calls = 0

    def extract(self, text: str) -> list[RawEvent]:
        self.calls += 1
        return self.result


class FakeSink:
    def __init__(self) -> None:
        self.created: list[TradingEvent] = []
        self.deleted: list[str] = []
        self.existing_by_key: dict[str, str] = {}
        self._next_id = 0

    def find_event_id_by_key(self, event_key: str) -> str | None:
        return self.existing_by_key.get(event_key)

    def create_event(self, event: TradingEvent) -> str:
        self.created.append(event)
        self._next_id += 1
        return f"gid{self._next_id}"

    def delete_event(self, event_id: str) -> None:
        self.deleted.append(event_id)


def test_new_post_creates_events_and_updates_state(tmp_path: Path) -> None:
    sink, state = FakeSink(), State()
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW]),
        sink=sink,
        state=state,
        config=make_config(tmp_path),
        now=NOW,
    )
    assert report.events_created == 1
    assert len(sink.created) == 1
    tracked = state.posts[POST.post_key]
    assert tracked.content_hash == POST.content_hash
    assert tracked.events[0].google_event_id == "gid1"
    # v2: display data captured for ICS export
    assert tracked.events[0].summary == sink.created[0].summary
    assert tracked.events[0].start == "2026-06-06T08:00:00+03:00"


def test_unchanged_post_skips_llm(tmp_path: Path) -> None:
    extractor = FakeExtractor([RAW])
    state = State(
        posts={
            POST.post_key: PostState(
                content_hash=POST.content_hash,
                last_seen="2026-05-31T00:00:00+00:00",
                events=[TrackedEvent("k", "g", "2026-06-06T14:00:00+03:00")],
            )
        }
    )
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=extractor,
        sink=FakeSink(),
        state=state,
        config=make_config(tmp_path),
        now=NOW,
    )
    assert extractor.calls == 0
    assert report.posts_skipped_unchanged == 1
    assert state.posts[POST.post_key].last_seen == NOW.isoformat()


def test_changed_post_reconciles(tmp_path: Path) -> None:
    """A rescheduled announcement deletes the future stale event and creates the new one."""
    sink = FakeSink()
    state = State(
        posts={
            POST.post_key: PostState(
                content_hash="old-hash",
                last_seen="2026-05-31T00:00:00+00:00",
                events=[TrackedEvent("stale-key", "stale-gid", "2026-06-07T14:00:00+03:00")],
            )
        }
    )
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW]),
        sink=sink,
        state=state,
        config=make_config(tmp_path),
        now=NOW,
    )
    assert sink.deleted == ["stale-gid"]
    assert report.events_created == 1 and report.events_deleted == 1
    keys = [e.event_key for e in state.posts[POST.post_key].events]
    assert "stale-key" not in keys


def test_ended_events_are_never_deleted(tmp_path: Path) -> None:
    sink = FakeSink()
    state = State(
        posts={
            POST.post_key: PostState(
                content_hash="old-hash",
                last_seen="2026-05-31T00:00:00+00:00",
                events=[TrackedEvent("past-key", "past-gid", "2026-05-30T14:00:00+03:00")],
            )
        }
    )
    run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW]),
        sink=sink,
        state=state,
        config=make_config(tmp_path),
        now=NOW,
    )
    assert sink.deleted == []
    keys = [e.event_key for e in state.posts[POST.post_key].events]
    assert "past-key" in keys  # history preserved


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    sink, state = FakeSink(), State()
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW]),
        sink=sink,
        state=state,
        config=make_config(tmp_path),
        dry_run=True,
        now=NOW,
    )
    assert report.events_created == 1  # reported…
    assert sink.created == [] and state.posts == {}  # …but nothing performed


def test_irrelevant_post_skipped(tmp_path: Path) -> None:
    boring = SourcePost(post_key="p", title="t", text="nothing interesting here", url="u")
    extractor = FakeExtractor([RAW])
    report = run_pipeline(
        source=FakeSource([boring]),
        extractor=extractor,
        sink=FakeSink(),
        state=State(),
        config=make_config(tmp_path),
        now=NOW,
    )
    assert extractor.calls == 0
    assert report.posts_relevant == 0


def test_empty_extraction_never_deletes_future_events(tmp_path: Path) -> None:
    """A degraded extraction must not wipe correct future events.

    FTMO fixing a typo, a truncated fetch or a prompt regression all look
    identical to a genuine withdrawal — and are far more likely. Deleting is
    irreversible for a subscriber who has already planned around the window.
    """
    sink = FakeSink()
    state = State(
        posts={
            POST.post_key: PostState(
                content_hash="old-hash",
                last_seen="2026-05-31T00:00:00+00:00",
                events=[
                    TrackedEvent("k1", "gid1", "2026-06-06T14:00:00+03:00", summary="Maintenance"),
                    TrackedEvent("k2", "gid2", "2026-06-07T14:00:00+03:00", summary="Crypto"),
                ],
            )
        }
    )
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([]),  # extraction collapsed to nothing
        sink=sink,
        state=state,
        config=make_config(tmp_path),
        now=NOW,
    )
    assert sink.deleted == []
    assert report.events_deleted == 0
    assert {e.event_key for e in state.posts[POST.post_key].events} == {"k1", "k2"}
    assert report.anomalies and "refusing to delete" in report.anomalies[0]


def test_empty_extraction_can_delete_when_explicitly_allowed(tmp_path: Path) -> None:
    sink = FakeSink()
    config = make_config(tmp_path)
    config = dataclasses.replace(
        config, events=dataclasses.replace(config.events, delete_on_empty_extraction=True)
    )
    state = State(
        posts={
            POST.post_key: PostState(
                content_hash="old-hash",
                last_seen="2026-05-31T00:00:00+00:00",
                events=[TrackedEvent("k1", "gid1", "2026-06-06T14:00:00+03:00")],
            )
        }
    )
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([]),
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    assert sink.deleted == ["gid1"]
    assert report.events_deleted == 1
    assert report.anomalies == []


def test_a_post_that_never_had_events_is_not_an_anomaly(tmp_path: Path) -> None:
    """Most announcements schedule nothing; that is normal, not a regression."""
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([]),
        sink=FakeSink(),
        state=State(),
        config=make_config(tmp_path),
        now=NOW,
    )
    assert report.anomalies == []


def test_keyword_gate_matching_nothing_is_an_anomaly(tmp_path: Path) -> None:
    """Posts exist but none is relevant: the wording or the page moved.

    Without this the run exits 0, the heartbeat says alive, and the calendar
    quietly empties as tracked events age out.
    """
    reworded = SourcePost(
        post_key="p1",
        title="Trading Update",
        text="Scheduled downtime is planned for the platform this weekend.",
        url="u",
    )
    report = run_pipeline(
        source=FakeSource([reworded]),
        extractor=FakeExtractor([RAW]),
        sink=FakeSink(),
        state=State(),
        config=make_config(tmp_path),
        now=NOW,
    )
    assert report.posts_seen == 1 and report.posts_relevant == 0
    assert report.anomalies and "keyword gate" in report.anomalies[0]
    assert "anomaly" in report.summary()


def test_no_posts_at_all_is_not_a_keyword_anomaly(tmp_path: Path) -> None:
    """An empty scrape is the scraper's error to raise, not the gate's."""
    report = run_pipeline(
        source=FakeSource([]),
        extractor=FakeExtractor([RAW]),
        sink=FakeSink(),
        state=State(),
        config=make_config(tmp_path),
        now=NOW,
    )
    assert report.anomalies == []


def test_some_relevant_posts_is_not_an_anomaly(tmp_path: Path) -> None:
    boring = SourcePost(post_key="p2", title="t", text="nothing interesting here", url="u")
    report = run_pipeline(
        source=FakeSource([POST, boring]),
        extractor=FakeExtractor([RAW]),
        sink=FakeSink(),
        state=State(),
        config=make_config(tmp_path),
        now=NOW,
    )
    assert report.posts_seen == 2 and report.posts_relevant == 1
    assert report.anomalies == []


def test_calendar_recovery_via_key_lookup(tmp_path: Path) -> None:
    """State lost but the event already exists in the calendar -> reuse, don't duplicate."""
    sink = FakeSink()
    config = make_config(tmp_path)
    # Compute the real event key by running once, then simulate state loss.
    state = State()
    run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW]),
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    key = state.posts[POST.post_key].events[0].event_key
    sink2 = FakeSink()
    sink2.existing_by_key[key] = "preexisting-gid"
    fresh_state = State()
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW]),
        sink=sink2,
        state=fresh_state,
        config=config,
        now=NOW,
    )
    assert sink2.created == []
    assert report.events_kept == 1
    assert fresh_state.posts[POST.post_key].events[0].google_event_id == "preexisting-gid"
