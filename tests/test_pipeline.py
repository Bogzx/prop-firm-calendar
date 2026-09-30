import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from prop_firm_calendar.config import AppConfig, CalendarConfig, EventRules, LLMConfig, SourceConfig
from prop_firm_calendar.models import SourcePost, TradingEvent
from prop_firm_calendar.parsing.llm import RawEvent
from prop_firm_calendar.parsing.validate import validate_events
from prop_firm_calendar.pipeline import run_pipeline
from prop_firm_calendar.state import PostState, State, TrackedEvent, load_state, save_state

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

RAW_B = RawEvent(
    event_type="crypto_closure",
    start_time="2026-06-07T00:00:00",
    end_time="2026-06-07T23:59:00",
    stated_utc_offset="+03:00",
)

RAW_C = RawEvent(
    event_type="early_close",
    start_time="2026-06-08T20:00:00",
    end_time="2026-06-08T23:59:00",
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


def test_partial_collapse_refuses_to_delete_the_missing_events(tmp_path: Path) -> None:
    """The refusal must not be bypassable by a partial-but-degraded extraction.

    8 events becoming 1 is a degraded extraction (truncated fetch, consensus
    flicker), not seven simultaneous withdrawals. If only the all-the-way-to-
    zero case refused, a shrink to any non-empty subset would silently delete
    every missing future event — the exact hazard the refusal exists for.
    """
    sink, state = FakeSink(), State()
    config = make_config(tmp_path)
    run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW, RAW_B, RAW_C]),
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    keys = {e.event_key for e in state.posts[POST.post_key].events}
    assert len(keys) == 3

    changed = dataclasses.replace(POST, text=POST.text + " (edited)")
    report = run_pipeline(
        source=FakeSource([changed]),
        extractor=FakeExtractor([RAW]),  # degraded: a strict subset survives
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    assert sink.deleted == []
    assert report.events_deleted == 0
    assert {e.event_key for e in state.posts[POST.post_key].events} == keys
    assert report.anomalies and "refusing to delete" in report.anomalies[0]


def test_partial_collapse_can_delete_when_explicitly_allowed(tmp_path: Path) -> None:
    sink, state = FakeSink(), State()
    config = make_config(tmp_path)
    config = dataclasses.replace(
        config, events=dataclasses.replace(config.events, delete_on_empty_extraction=True)
    )
    run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW, RAW_B]),
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    changed = dataclasses.replace(POST, text=POST.text + " (edited)")
    report = run_pipeline(
        source=FakeSource([changed]),
        extractor=FakeExtractor([RAW]),
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    assert len(sink.deleted) == 1
    assert report.events_deleted == 1
    assert report.anomalies == []


def test_shrink_with_a_genuinely_new_event_still_reconciles(tmp_path: Path) -> None:
    """A reschedule announces new times; that is evidence, not doubt.

    Extraction that loses an old event but gains a new one is what a real
    announcement change looks like, so normal reconcile applies: the stale
    future event goes, the new one is created.
    """
    sink, state = FakeSink(), State()
    config = make_config(tmp_path)
    run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW, RAW_B]),
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    changed = dataclasses.replace(POST, text=POST.text + " (rescheduled)")
    report = run_pipeline(
        source=FakeSource([changed]),
        extractor=FakeExtractor([RAW, RAW_C]),  # RAW_B withdrawn, RAW_C announced
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    assert len(sink.deleted) == 1
    assert report.events_deleted == 1 and report.events_created == 1
    assert report.anomalies == []


def test_zero_extraction_over_only_past_events_is_not_an_anomaly(tmp_path: Path) -> None:
    """An edit to a post whose events already happened puts nothing at risk.

    Past events are never deleted anyway, so refusing (and paging a human via
    503) would be pure noise — e.g. FTMO touching an old post's footer after
    the window passed.
    """
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
    report = run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([]),
        sink=sink,
        state=state,
        config=make_config(tmp_path),
        now=NOW,
    )
    assert report.anomalies == []
    assert sink.deleted == []
    assert {e.event_key for e in state.posts[POST.post_key].events} == {"past-key"}


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


def test_the_post_url_is_recorded_even_when_the_post_is_unchanged(tmp_path: Path) -> None:
    """State written before v5 has no URL; the next sighting must fill it in."""
    state = State(
        posts={
            POST.post_key: PostState(
                content_hash=POST.content_hash, last_seen=NOW.isoformat(), events=[]
            )
        }
    )
    extractor = FakeExtractor([RAW])
    run_pipeline(
        source=FakeSource([POST]),
        extractor=extractor,
        sink=FakeSink(),
        state=state,
        config=make_config(tmp_path),
        now=NOW,
    )
    assert extractor.calls == 0
    assert state.posts[POST.post_key].url == POST.url


def test_a_new_post_records_its_url(tmp_path: Path) -> None:
    state = State()
    run_pipeline(
        source=FakeSource([POST]),
        extractor=FakeExtractor([RAW]),
        sink=FakeSink(),
        state=state,
        config=make_config(tmp_path),
        now=NOW,
    )
    assert state.posts[POST.post_key].url == POST.url


# -- events beyond max_days_ahead -----------------------------------------

FAR = RawEvent(
    event_type="holiday_closure",
    start_time="2026-12-25T00:00:00",
    end_time="2026-12-25T23:59:00",
    stated_utc_offset="+03:00",
)
LATER = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)  # Dec 25 is now 115 days out


def _run(tmp_path: Path, state: State, extractor, now: datetime, posts=None, sink=None, **kw):
    return run_pipeline(
        source=FakeSource([POST] if posts is None else posts),
        extractor=extractor,
        sink=sink or FakeSink(),
        state=state,
        config=make_config(tmp_path),
        now=now,
        firm="ftmo",
        **kw,
    )


def test_a_far_future_event_is_deferred_not_forgotten(tmp_path: Path) -> None:
    state, sink = State(), FakeSink()
    report = _run(tmp_path, state, FakeExtractor([RAW, FAR]), NOW, sink=sink)
    assert len(sink.created) == 1  # only the near event
    assert report.events_deferred == 1
    assert report.rejections == 0  # held back, not rejected
    assert "1 deferred" in report.summary()
    assert state.posts[POST.post_key].deferred == [FAR.model_dump()]


def test_a_deferred_event_is_published_once_in_range_without_an_llm_call(tmp_path: Path) -> None:
    """Regression: an unchanged post skipped extraction, so Dec 25 never arrived."""
    state = State()
    _run(tmp_path, state, FakeExtractor([RAW, FAR]), NOW)

    extractor, sink = FakeExtractor([]), FakeSink()
    report = _run(tmp_path, state, extractor, LATER, sink=sink)
    assert extractor.calls == 0
    assert [e.start.date().isoformat() for e in sink.created] == ["2026-12-25"]
    assert report.events_created == 1
    post_state = state.posts[POST.post_key]
    assert post_state.deferred == []
    # Same identity it would have had if extracted fresh on LATER.
    fresh, _ = validate_events(
        [FAR], POST, EventRules(), ZoneInfo("Etc/GMT-3"), ZoneInfo("Etc/GMT-3"), now=LATER
    )
    assert fresh[0].event_key in {e.event_key for e in post_state.events}

    # And it is not published twice.
    again = FakeSink()
    _run(tmp_path, state, FakeExtractor([]), LATER, sink=again)
    assert again.created == []


def test_deferred_events_survive_the_post_leaving_the_index_page(tmp_path: Path) -> None:
    """FTMO lists only recent posts; the announcement may be gone by then."""
    state = State()
    _run(tmp_path, state, FakeExtractor([RAW, FAR]), NOW)
    other = SourcePost("trading-update-2026-08-30", "t", "maintenance soon", POST.url)
    sink = FakeSink()
    _run(tmp_path, state, FakeExtractor([]), LATER, posts=[other], sink=sink)
    assert [e.start.date().isoformat() for e in sink.created] == ["2026-12-25"]
    assert sink.created[0].source_url == POST.url


def test_deferred_events_are_only_promoted_by_their_own_firm(tmp_path: Path) -> None:
    state = State()
    _run(tmp_path, state, FakeExtractor([RAW, FAR]), NOW)
    sink = FakeSink()
    run_pipeline(
        source=FakeSource([]),
        extractor=FakeExtractor([]),
        sink=sink,
        state=state,
        config=make_config(tmp_path),
        now=LATER,
        firm="topstep",
        source_timezone="America/Chicago",
    )
    assert sink.created == []
    assert state.posts[POST.post_key].deferred == [FAR.model_dump()]


def test_a_dry_run_does_not_consume_deferred_events(tmp_path: Path) -> None:
    state = State()
    _run(tmp_path, state, FakeExtractor([RAW, FAR]), NOW)
    report = _run(tmp_path, state, FakeExtractor([]), LATER, dry_run=True)
    assert report.events_created == 1
    assert state.posts[POST.post_key].deferred == [FAR.model_dump()]


def test_a_pre_v5_post_is_re_extracted_exactly_once(tmp_path: Path) -> None:
    """Old state cannot say what it dropped; one extraction recovers it."""
    path = tmp_path / "state.json"
    state = State()
    _run(tmp_path, state, FakeExtractor([RAW]), NOW)
    save_state(state, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for post in payload["posts"].values():
        del post["deferred"]  # what a v4 file looks like
    payload["version"] = 4
    path.write_text(json.dumps(payload), encoding="utf-8")

    upgraded = load_state(path)
    assert upgraded.posts[POST.post_key].deferred is None
    extractor, sink = FakeExtractor([RAW, FAR]), FakeSink()
    _run(tmp_path, upgraded, extractor, LATER, sink=sink)
    assert extractor.calls == 1
    assert [e.start.date().isoformat() for e in sink.created] == ["2026-12-25"]

    _run(tmp_path, upgraded, extractor, LATER)
    assert extractor.calls == 1


def _as_v4(tmp_path: Path, state: State) -> State:
    path = tmp_path / "v4.json"
    save_state(state, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for post in payload["posts"].values():
        del post["deferred"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_state(path)


def test_the_upgrade_re_extraction_never_replaces_a_tracked_event(tmp_path: Path) -> None:
    """Review: the text is unchanged, so a drifted reading is not a reschedule.

    Reconciling it deleted the calendar entry and gave subscribers a new UID
    for every tracked window the live model happened to read differently.
    """
    first = FakeSink()
    state = State()
    _run(tmp_path, state, FakeExtractor([RAW, RAW_B]), NOW, sink=first)
    before = [(e.event_key, e.google_event_id) for e in state.posts[POST.post_key].events]
    upgraded = _as_v4(tmp_path, state)

    drifted = RAW.model_copy(update={"end_time": "2026-06-06T14:30:00"})
    sink = FakeSink()
    report = _run(tmp_path, upgraded, FakeExtractor([drifted, FAR]), NOW, sink=sink)
    assert sink.deleted == [] and sink.created == []
    assert report.events_deleted == 0 and report.anomalies == []
    post = upgraded.posts[POST.post_key]
    # RAW_B missing from this reading is not a withdrawal either.
    assert [(e.event_key, e.google_event_id) for e in post.events] == before
    assert post.deferred == [FAR.model_dump()]


def test_the_upgrade_re_extraction_adds_what_the_old_cap_dropped(tmp_path: Path) -> None:
    state = State()
    _run(tmp_path, state, FakeExtractor([RAW]), NOW)
    upgraded = _as_v4(tmp_path, state)
    sink = FakeSink()
    # By LATER the dropped far event is within range: it is published directly.
    _run(tmp_path, upgraded, FakeExtractor([RAW, FAR]), LATER, sink=sink)
    assert [e.start.date().isoformat() for e in sink.created] == ["2026-12-25"]
    assert len(upgraded.posts[POST.post_key].events) == 2


def test_prune_keeps_a_post_that_still_holds_deferred_events() -> None:
    old = "2026-01-01T00:00:00+00:00"
    state = State(
        posts={
            "held": PostState("h", old, [], deferred=[FAR.model_dump()]),
            "done": PostState("h", old, []),
        }
    )
    state.prune(now=NOW)
    assert list(state.posts) == ["held"]


# -- rejections reach /healthz --------------------------------------------


def test_a_rejection_that_drops_a_real_event_is_an_anomaly(tmp_path: Path) -> None:
    """Regression: rejections were a logger.warning and /healthz stayed green."""
    backwards = RawEvent(
        event_type="maintenance",
        start_time="2026-06-06T14:00:00",
        end_time="2026-06-06T08:00:00",
        stated_utc_offset="+03:00",
        affected="cTrader",
    )
    state = State()
    report = _run(tmp_path, state, FakeExtractor([RAW, backwards]), NOW, display_name="FTMO")
    assert report.rejections == 1
    [anomaly] = report.anomalies
    assert anomaly.startswith("FTMO: post trading-update-2026-06-04: 1 extracted event(s)")
    assert "end is not after start" in anomaly and "cTrader" in anomaly
    assert state.posts[POST.post_key].rejected == [
        "maintenance 2026-06-06T14:00:00 (cTrader): end is not after start"
    ]


def test_a_missing_stated_offset_is_an_anomaly_not_a_silent_zero(tmp_path: Path) -> None:
    """How E8 could read 'ok' with 0 events: every row rejected, nothing raised."""
    no_offset = RAW.model_copy(update={"stated_utc_offset": None})
    report = _run(tmp_path, State(), FakeExtractor([no_offset]), NOW, require_stated_offset=True)
    assert report.events_created == 0
    assert report.anomalies and "refusing to guess the hour" in report.anomalies[0]


def test_benign_rejections_raise_nothing(tmp_path: Path) -> None:
    ended = RawEvent(
        event_type="maintenance",
        start_time="2026-05-01T08:00:00",
        end_time="2026-05-01T09:00:00",
        stated_utc_offset="+03:00",
    )
    state = State()
    report = _run(tmp_path, state, FakeExtractor([RAW, ended]), NOW)
    assert report.rejections == 1
    assert report.anomalies == []
    assert state.posts[POST.post_key].rejected == []


# -- one calendar entry per window, shared across posts -------------------

FOLLOW_UP = SourcePost(
    post_key="trading-update-2026-06-05",
    title="Trading Update | Jun 5 2026",
    text="reminder: ctrader maintenance on Saturday 6 Jun 2026 08:00 to 14:00 GMT+3",
    url="https://ftmo.com/en/blog/trading-updates/trading-update-5-jun-2026/",
)


class PerPostExtractor:
    """Returns a different extraction per post text."""

    def __init__(self, by_text: dict[str, list[RawEvent]]) -> None:
        self.by_text = by_text
        self.calls = 0

    def extract(self, text: str) -> list[RawEvent]:
        self.calls += 1
        return self.by_text[text]


def _sync(tmp_path: Path, state: State, sink: FakeSink, by_post: dict, now: datetime = NOW):
    posts = list(by_post)
    extractor = PerPostExtractor({p.text: events for p, events in by_post.items()})
    return _run(tmp_path, state, extractor, now, posts=posts, sink=sink)


def test_a_window_announced_twice_gets_one_calendar_entry(tmp_path: Path) -> None:
    state, sink = State(), FakeSink()
    report = _sync(tmp_path, state, sink, {POST: [RAW], FOLLOW_UP: [RAW]})
    assert len(sink.created) == 1
    assert report.events_created == 1 and report.events_kept == 1
    first = state.posts[POST.post_key].events[0]
    second = state.posts[FOLLOW_UP.post_key].events[0]
    assert first.event_key != second.event_key  # identity stays per post
    assert first.google_event_id == second.google_event_id


def test_a_shared_entry_is_deleted_only_when_the_last_post_withdraws(tmp_path: Path) -> None:
    state, sink = State(), FakeSink()
    _sync(tmp_path, state, sink, {POST: [RAW], FOLLOW_UP: [RAW]})
    [shared_id] = {e.google_event_id for p in state.posts.values() for e in p.events}

    # The first post is edited: the window moves out of it (a new event
    # arrives, so this is a genuine change, not a degraded extraction).
    edited = dataclasses.replace(POST, text=POST.text + " (updated)")
    report = _sync(tmp_path, state, sink, {edited: [RAW_B], FOLLOW_UP: [RAW]})
    assert sink.deleted == []
    assert report.events_deleted == 0

    # Now the follow-up drops it too: that is the last reference.
    follow_edit = dataclasses.replace(FOLLOW_UP, text=FOLLOW_UP.text + " (updated)")
    report = _sync(tmp_path, state, sink, {edited: [RAW_B], follow_edit: [RAW_C]})
    assert sink.deleted == [shared_id]
    assert report.events_deleted == 1


def test_both_posts_withdrawing_in_one_run_deletes_once(tmp_path: Path) -> None:
    state, sink = State(), FakeSink()
    _sync(tmp_path, state, sink, {POST: [RAW], FOLLOW_UP: [RAW]})
    a = dataclasses.replace(POST, text=POST.text + " v2")
    b = dataclasses.replace(FOLLOW_UP, text=FOLLOW_UP.text + " v2")
    _sync(tmp_path, state, sink, {a: [RAW_B], b: [RAW_C]})
    assert len(sink.deleted) == 1


def test_different_symbols_at_the_same_minute_are_not_shared(tmp_path: Path) -> None:
    other = RAW.model_copy(update={"affected": "GER40.cash"})
    state, sink = State(), FakeSink()
    _sync(tmp_path, state, sink, {POST: [RAW], FOLLOW_UP: [other]})
    assert len(sink.created) == 2


def test_windows_are_not_shared_across_firms(tmp_path: Path) -> None:
    state, sink = State(), FakeSink()
    _sync(tmp_path, state, sink, {POST: [RAW]})
    run_pipeline(
        source=FakeSource([FOLLOW_UP]),
        extractor=FakeExtractor([RAW]),
        sink=sink,
        state=state,
        config=make_config(tmp_path),
        now=NOW,
        firm="other-firm",
    )
    assert len(sink.created) == 2


def _legacy_duplicate_state() -> State:
    """What 0.8/0.9-pre state looks like: two Google events for one window."""
    events, _ = validate_events(
        [RAW], POST, EventRules(), ZoneInfo("Etc/GMT-3"), ZoneInfo("Etc/GMT-3"), now=NOW
    )
    twin, _ = validate_events(
        [RAW], FOLLOW_UP, EventRules(), ZoneInfo("Etc/GMT-3"), ZoneInfo("Etc/GMT-3"), now=NOW
    )

    def tracked(event, gid: str) -> TrackedEvent:
        return TrackedEvent(
            event.event_key,
            gid,
            event.end.isoformat(),
            summary=event.summary,
            start=event.start.isoformat(),
            event_type=event.event_type.value,
        )

    return State(
        posts={
            POST.post_key: PostState(
                POST.content_hash, NOW.isoformat(), [tracked(events[0], "g-old")], firm="ftmo"
            ),
            FOLLOW_UP.post_key: PostState(
                FOLLOW_UP.content_hash, NOW.isoformat(), [tracked(twin[0], "g-dup")], firm="ftmo"
            ),
        }
    )


def test_existing_duplicates_are_merged_into_the_oldest_entry(tmp_path: Path) -> None:
    """The migration: duplicates created before sharing collapse on the next run."""
    state, sink = _legacy_duplicate_state(), FakeSink()
    report = _sync(tmp_path, state, sink, {POST: [RAW], FOLLOW_UP: [RAW]})
    assert sink.deleted == ["g-dup"]
    assert sink.created == []
    assert report.duplicates_merged == 1
    assert "1 duplicate calendar entries merged" in report.summary()
    ids = {e.google_event_id for p in state.posts.values() for e in p.events}
    assert ids == {"g-old"}

    # Idempotent: nothing left to merge.
    again = FakeSink()
    assert _sync(tmp_path, state, again, {POST: [RAW], FOLLOW_UP: [RAW]}).duplicates_merged == 0
    assert again.deleted == []


def test_the_merge_survives_a_save_and_load(tmp_path: Path) -> None:
    state, sink = _legacy_duplicate_state(), FakeSink()
    _sync(tmp_path, state, sink, {POST: [RAW], FOLLOW_UP: [RAW]})
    save_state(state, tmp_path / "state.json")
    loaded = load_state(tmp_path / "state.json")
    assert {e.google_event_id for p in loaded.posts.values() for e in p.events} == {"g-old"}


def test_a_failed_merge_delete_keeps_the_duplicate_and_the_sync(tmp_path: Path) -> None:
    class FlakySink(FakeSink):
        def delete_event(self, event_id: str) -> None:
            raise RuntimeError("Google 500")

    state = _legacy_duplicate_state()
    report = _sync(tmp_path, state, FlakySink(), {POST: [RAW], FOLLOW_UP: [RAW]})
    assert report.duplicates_merged == 0
    ids = {e.google_event_id for p in state.posts.values() for e in p.events}
    assert ids == {"g-old", "g-dup"}  # untouched, retried next run


def test_a_dry_run_merges_nothing(tmp_path: Path) -> None:
    state, sink = _legacy_duplicate_state(), FakeSink()
    extractor = PerPostExtractor({POST.text: [RAW], FOLLOW_UP.text: [RAW]})
    _run(tmp_path, state, extractor, NOW, posts=[POST, FOLLOW_UP], sink=sink, dry_run=True)
    assert sink.deleted == []


def _repoint(state: State, post_key: str, backend_id: str) -> None:
    for tracked in state.posts[post_key].events:
        tracked.google_event_id = backend_id


def test_a_feed_only_placeholder_never_outlives_a_real_google_entry(tmp_path: Path) -> None:
    """Review: after switching feed-only -> Google, the merge deleted the real entry.

    The older post's `ics:` id "survived", the real Google event was deleted
    and the window vanished from the user's calendar.
    """
    state = _legacy_duplicate_state()
    _repoint(state, POST.post_key, "ics:whatever")
    sink = FakeSink()
    report = _sync(tmp_path, state, sink, {POST: [RAW], FOLLOW_UP: [RAW]})
    assert sink.deleted == []
    assert report.duplicates_merged == 1
    assert {e.google_event_id for p in state.posts.values() for e in p.events} == {"g-dup"}


def test_google_mode_does_not_share_a_placeholder(tmp_path: Path) -> None:
    """A new post matching a feed-only entry gets a real calendar entry."""
    state, sink = State(), FakeSink()
    _sync(tmp_path, state, sink, {POST: [RAW]})
    _repoint(state, POST.post_key, "ics:whatever")
    sink = FakeSink()
    _sync(tmp_path, state, sink, {POST: [RAW], FOLLOW_UP: [RAW]})
    assert len(sink.created) == 1
    # ...and the merge then points the old post at it too.
    assert {e.google_event_id for p in state.posts.values() for e in p.events} == {"gid1"}


def test_feed_only_mode_still_shares_placeholders(tmp_path: Path) -> None:
    from prop_firm_calendar.sinks.null import StateOnlySink

    state = State()
    extractor = PerPostExtractor({POST.text: [RAW], FOLLOW_UP.text: [RAW]})
    _run(tmp_path, state, extractor, NOW, posts=[POST, FOLLOW_UP], sink=StateOnlySink())
    ids = {e.google_event_id for p in state.posts.values() for e in p.events}
    assert len(ids) == 1 and next(iter(ids)).startswith("ics:")


def test_the_tracked_event_keeps_its_evidence_through_a_save(tmp_path: Path) -> None:
    quoted = RAW.model_copy(update={"evidence": "ctrader maintenance on Saturday 6 Jun 2026"})
    state = State()
    _run(tmp_path, state, FakeExtractor([quoted]), NOW)
    save_state(state, tmp_path / "state.json")
    [tracked] = load_state(tmp_path / "state.json").posts[POST.post_key].events
    assert tracked.evidence == "ctrader maintenance on Saturday 6 Jun 2026"
