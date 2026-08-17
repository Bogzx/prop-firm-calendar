"""Golden test: one real announcement, pinned end-to-end to its calendar events.

CHANGELOG 0.8.0 records that the Memorial Day / Buddha's Birthday post of
21 May 2026 "extracts as 8 correctly-typed events with zero rejections". That
was checked by hand against a live model and then written down nowhere, so
every later prompt tweak, taxonomy change or validation edit risked regressing
it silently.

This pins the whole deterministic path:

    recorded HTML  ->  scraped announcement text   (sources/web.py)
    pinned RawEvents  ->  8 TradingEvents          (parsing/validate.py)
    3 identical runs  ->  the same 8 events        (parsing/llm.py consensus)

What it deliberately does not do is call a model: CI has no API key, and a test
that needs one is a test that gets skipped. The model's half of the contract is
pinned as data instead — trading-update-21-may-2026.expected.json is what a
correct extraction of this text looks like, reviewable in a diff. If the prompt
changes such that a model would no longer produce it, that JSON has to change
too, and the change is visible.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ftmo_calendar.config import FTMO_PLATFORM_TZ, EventRules
from ftmo_calendar.models import EventType
from ftmo_calendar.parsing.llm import EventExtractor, RawEvent
from ftmo_calendar.parsing.validate import validate_events
from ftmo_calendar.pipeline import _is_relevant
from ftmo_calendar.sources.ftmo import FtmoSource

FIXTURES = Path(__file__).parent / "fixtures" / "ftmo"
URL = "https://ftmo.com/en/blog/trading-updates/trading-update-21-may-2026/"
TZ = ZoneInfo(FTMO_PLATFORM_TZ)

# A moment after the announcement was published and before its first event.
NOW = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)

POST = FtmoSource().parse_post(
    (FIXTURES / "trading-update-21-may-2026.html").read_text(encoding="utf-8"), URL
)
GOLDEN = json.loads(
    (FIXTURES / "trading-update-21-may-2026.expected.json").read_text(encoding="utf-8")
)
RAW_EVENTS = [RawEvent(**e) for e in GOLDEN["events"]]

# (summary, start, end) — FTMO's stated GMT+3 wall clock, exactly as announced.
EXPECTED = [
    ("⚠️ Platform Maintenance — MT4, MT5, cTrader, DXtrade", "2026-05-23T17:00:00", "21:00:00"),
    (
        "⏳ Early Close — JP225.cash, US30.cash, US100.cash, US500.cash, US2000.cash",
        "2026-05-25T20:00:00",
        "23:59:00",
    ),
    (
        "⏳ Early Close — Metals CFD, USOIL.cash, HEATOIL.c, NATGAS.cash",
        "2026-05-25T21:30:00",
        "23:59:00",
    ),
    (
        "🏖️ Closed All Day — UK100.cash, HK50.cash, Equities I CFD, Agriculture",
        "2026-05-25T00:00:00",
        "23:59:00",
    ),
    ("⏳ Early Close — GER40.cash", "2026-05-25T23:00:00", "23:59:00"),
    ("⏳ Early Close — UKOIL.cash", "2026-05-25T20:30:00", "23:59:00"),
    ("🕗 Late Open — UK100.cash", "2026-05-26T00:00:00", "03:05:00"),
    ("🕗 Late Open — CORN.c, SOYBEAN.c, WHEAT.c", "2026-05-26T00:00:00", "16:35:00"),
]


def _validate(rules: EventRules | None = None):
    return validate_events(RAW_EVENTS, POST, rules or EventRules(), TZ, TZ, now=NOW)


# -- the announcement text ------------------------------------------------


def test_scraped_text_still_contains_every_announced_fact() -> None:
    """If the scraper starts reading a different element, this fails first."""
    text = POST.text
    assert "GMT+3" in text
    assert "Memorial Day" in text and "Buddha" in text
    for symbol in ("JP225.cash", "UK100.cash", "GER40.cash", "UKOIL.cash", "CORN.c", "NATGAS.cash"):
        assert symbol in text, f"{symbol} missing from the scraped announcement"
    for platform in ("MT4", "MT5", "cTrader", "DXtrade"):
        assert platform in text


def test_announcement_passes_the_keyword_gate() -> None:
    """The gate that decides whether the LLM is called at all."""
    from ftmo_calendar.config import SourceConfig

    assert _is_relevant(POST, SourceConfig().keywords)


# -- extraction -> events -------------------------------------------------


def test_eight_events_zero_rejections() -> None:
    """The claim in CHANGELOG 0.8.0, now enforced."""
    events, rejections = _validate()
    assert rejections == []
    assert len(events) == 8


def test_every_event_matches_its_announced_wall_clock() -> None:
    events, _ = _validate()
    actual = [
        (e.summary, e.start.strftime("%Y-%m-%dT%H:%M:%S"), e.end.strftime("%H:%M:%S"))
        for e in events
    ]
    assert actual == EXPECTED


def test_event_type_distribution() -> None:
    """One maintenance window, four early closes, one all-day closure, two late opens."""
    events, _ = _validate()
    counts: dict[EventType, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    assert counts == {
        EventType.MAINTENANCE: 1,
        EventType.EARLY_CLOSE: 4,
        EventType.HOLIDAY_CLOSURE: 1,
        EventType.LATE_OPEN: 2,
    }


def test_times_are_fixed_gmt3_not_a_dst_zone() -> None:
    """Announced times must survive as +03:00 whatever the season.

    Europe/Bucharest equals GMT+3 only from late March to late October. Using
    it shifted every offset-less winter announcement an hour early.
    """
    events, _ = _validate()
    assert {e.start.utcoffset() for e in events} == {datetime.now(TZ).utcoffset()}
    assert all(e.start.strftime("%z") == "+0300" for e in events)


def test_every_event_links_back_to_the_source_post() -> None:
    events, _ = _validate()
    assert all(e.source_post_key == "trading-update-2026-05-21" for e in events)
    assert all(URL in e.description for e in events)


def test_event_keys_are_unique_and_stable() -> None:
    """Reconcile identity: 8 distinct keys, unchanged across re-validation."""
    first, _ = _validate()
    second, _ = _validate()
    keys = [e.event_key for e in first]
    assert len(set(keys)) == 8
    assert keys == [e.event_key for e in second]


# -- consensus ------------------------------------------------------------


class ScriptedBackend:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    def complete(self, prompt: str, model: str) -> str:
        return self.replies.pop(0)


def test_consensus_over_three_identical_runs_keeps_all_eight() -> None:
    reply = json.dumps(GOLDEN["events"])
    extractor = EventExtractor(ScriptedBackend([reply] * 3), ["m"], consensus_runs=3)
    assert len(extractor.extract(POST.text)) == 8


def test_consensus_drops_an_event_only_one_run_saw() -> None:
    """A hallucinated extra window in one of three runs must not reach the feed."""
    hallucination = dict(GOLDEN["events"][0], start_time="2026-05-24T09:00:00")
    minority = json.dumps([*GOLDEN["events"], hallucination])
    majority = json.dumps(GOLDEN["events"])
    extractor = EventExtractor(
        ScriptedBackend([minority, majority, majority]), ["m"], consensus_runs=3
    )
    assert len(extractor.extract(POST.text)) == 8
