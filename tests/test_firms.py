"""Multi-firm orchestration: config, isolation, attribution, per-firm feeds.

The property that matters most here is isolation. With one source, a scrape
failure aborting the run was correct. With ten, it would mean one firm's
redesign freezes nine other firms' calendars — the same silent staleness this
project exists to prevent, reached from the other direction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ftmo_calendar.config import AppConfig, ConfigError, FirmConfig, load_config
from ftmo_calendar.firms import AllFirmsFailed, FirmOutcome, MultiRunReport, run_firms
from ftmo_calendar.models import SourcePost
from ftmo_calendar.parsing.llm import RawEvent
from ftmo_calendar.sinks.ics import render_ics
from ftmo_calendar.sinks.null import StateOnlySink
from ftmo_calendar.sources.factory import ResolvedFirm, resolve_firm
from ftmo_calendar.sources.profile import SourceProfile
from ftmo_calendar.state import State

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def write_config(tmp_path: Path, body: str) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(path, env={})


# -- configuration --------------------------------------------------------


def test_firms_array_is_read_in_order(tmp_path: Path) -> None:
    cfg = write_config(
        tmp_path,
        "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\n",
    )
    assert [f.profile for f in cfg.firms] == ["ftmo", "topstep"]


def test_a_firm_can_be_disabled_without_deleting_it(tmp_path: Path) -> None:
    cfg = write_config(
        tmp_path,
        "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\nenabled = false\n",
    )
    assert [f.profile for f in cfg.enabled_firms] == ["ftmo"]


def test_per_firm_overrides_beat_the_profile(tmp_path: Path) -> None:
    cfg = write_config(
        tmp_path,
        "[[firms]]\nprofile = 'topstep'\ntimezone = 'America/New_York'\nkeywords = ['x']\n",
    )
    resolved = resolve_firm(cfg.firms[0], cfg.scrape)
    assert resolved.timezone == "America/New_York"
    assert resolved.keywords == ("x",)


def test_a_firm_with_no_overrides_takes_the_profiles_settings(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, "[[firms]]\nprofile = 'topstep'\n")
    resolved = resolve_firm(cfg.firms[0], cfg.scrape)
    assert resolved.timezone == "America/Chicago"
    assert "holiday" in resolved.keywords


def test_an_unknown_profile_is_rejected_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown source profile"):
        write_config(tmp_path, "[[firms]]\nprofile = 'not-a-real-firm'\n")


def test_a_duplicated_firm_is_rejected(tmp_path: Path) -> None:
    """Two entries for one firm would scrape twice and collide on post keys."""
    with pytest.raises(ConfigError, match="twice"):
        write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'ftmo'\n")


def test_an_empty_firms_array_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="present but empty"):
        write_config(tmp_path, "firms = []\n")


def test_disabling_every_firm_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no firms are enabled"):
        write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\nenabled = false\n")


def test_an_invalid_per_firm_timezone_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid timezone"):
        write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\ntimezone = 'Mars/Olympus'\n")


# -- orchestration --------------------------------------------------------


class StubSource:
    def __init__(self, posts: list[SourcePost] | Exception) -> None:
        self.posts = posts

    def fetch(self) -> list[SourcePost]:
        if isinstance(self.posts, Exception):
            raise self.posts
        return list(self.posts)


class StubExtractor:
    def __init__(self, events: list[RawEvent] | None = None) -> None:
        self.events = events or []

    def extract(self, text: str) -> list[RawEvent]:
        return list(self.events)


def make_post(key: str, text: str = "scheduled maintenance window") -> SourcePost:
    return SourcePost(post_key=key, title=key, text=text, url=f"https://x.test/{key}")


def stub_resolver(mapping: dict[str, StubSource | Exception]):
    def resolve(firm: FirmConfig, scrape=None):  # noqa: ANN001, ARG001
        entry = mapping[firm.profile]
        if isinstance(entry, Exception):
            raise entry
        profile = SourceProfile(
            name=firm.profile,
            display_name=firm.profile.upper(),
            url="https://x.test/",
            post_content_selectors=("div",),
            keywords=("maintenance",),
        )
        return ResolvedFirm(
            profile=profile,
            source=entry,
            timezone=firm.timezone or "Etc/GMT-3",
            keywords=("maintenance",),
        )

    return resolve


def run(config: AppConfig, mapping, state: State | None = None) -> MultiRunReport:
    return run_firms(
        config=config,
        sink=StateOnlySink(),
        state=state if state is not None else State(),
        make_extractor=lambda resolved: StubExtractor(),
        now=NOW,
        resolve=stub_resolver(mapping),
        stagger_fn=lambda seconds: 0.0,
    )


def test_one_firm_failing_does_not_stop_the_others(tmp_path: Path) -> None:
    """The whole reason firms are isolated: nine calendars must keep updating."""
    cfg = write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\n")
    state = State()
    result = run(
        cfg,
        {
            "ftmo": StubSource(RuntimeError("ftmo.com redesigned")),
            "topstep": StubSource([make_post("ts-1")]),
        },
        state,
    )
    by_name = {o.name: o for o in result.outcomes}
    assert by_name["ftmo"].ok is False
    assert "redesigned" in by_name["ftmo"].error
    assert by_name["topstep"].ok is True
    assert by_name["topstep"].posts_seen == 1
    # And the healthy firm's work actually landed.
    assert state.posts["ts-1"].firm == "topstep"


def test_a_failing_firm_is_named_in_the_anomalies(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\n")
    result = run(
        cfg,
        {
            "ftmo": StubSource([make_post("f-1")]),
            "topstep": StubSource(RuntimeError("gone")),
        },
    )
    assert any("TOPSTEP" in a and "gone" in a for a in result.anomalies)


def test_when_every_firm_fails_the_run_fails(tmp_path: Path) -> None:
    """With one firm configured this is byte-for-byte the old behaviour."""
    cfg = write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\n")
    with pytest.raises(AllFirmsFailed, match="unreachable"):
        run(cfg, {"ftmo": StubSource(RuntimeError("unreachable"))})


def test_a_broken_profile_is_isolated_too(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\n")
    result = run(
        cfg,
        {
            "ftmo": ConfigError("profile is broken"),
            "topstep": StubSource([make_post("ts-1")]),
        },
    )
    by_name = {o.name: o for o in result.outcomes}
    assert by_name["ftmo"].ok is False
    assert by_name["topstep"].ok is True


def test_each_firm_stamps_its_own_posts(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\n")
    state = State()
    run(
        cfg,
        {
            "ftmo": StubSource([make_post("f-1")]),
            "topstep": StubSource([make_post("ts-1")]),
        },
        state,
    )
    assert state.posts["f-1"].firm == "ftmo"
    assert state.posts["ts-1"].firm == "topstep"


def test_firms_are_staggered(tmp_path: Path) -> None:
    """N firms on one interval must not all fire on the same second."""
    cfg = write_config(
        tmp_path,
        "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\n"
        "\n[[firms]]\nprofile = 'e8-markets'\n",
    )
    delays: list[float] = []
    run_firms(
        config=cfg,
        sink=StateOnlySink(),
        state=State(),
        make_extractor=lambda resolved: StubExtractor(),
        now=NOW,
        resolve=stub_resolver(
            {name: StubSource([make_post(name)]) for name in ("ftmo", "topstep", "e8-markets")}
        ),
        stagger_fn=lambda seconds: delays.append(seconds) or 0.0,
    )
    # First firm goes immediately; the rest are jittered.
    assert delays == [cfg.scrape.stagger_seconds, cfg.scrape.stagger_seconds]


def test_a_dry_run_does_not_stagger(tmp_path: Path) -> None:
    """Nothing is published, so nobody is waiting on politeness sleeps."""
    cfg = write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\n")
    delays: list[float] = []
    run_firms(
        config=cfg,
        sink=StateOnlySink(),
        state=State(),
        make_extractor=lambda resolved: StubExtractor(),
        dry_run=True,
        now=NOW,
        resolve=stub_resolver({n: StubSource([make_post(n)]) for n in ("ftmo", "topstep")}),
        stagger_fn=lambda seconds: delays.append(seconds) or 0.0,
    )
    assert delays == []


def test_totals_add_every_firm_up(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, "[[firms]]\nprofile = 'ftmo'\n\n[[firms]]\nprofile = 'topstep'\n")
    result = run(
        cfg,
        {
            "ftmo": StubSource([make_post("f-1"), make_post("f-2")]),
            "topstep": StubSource([make_post("ts-1")]),
        },
    )
    totals = result.totals()
    assert totals.posts_seen == 3
    assert totals.posts_relevant == 3


def test_a_run_report_labels_its_firm() -> None:
    outcome = FirmOutcome(name="topstep", display_name="Topstep", ok=True)
    assert outcome.as_dict()["status"] == "ok"
    assert (
        FirmOutcome(name="a", display_name="A", ok=False, error="x").as_dict()["status"] == "error"
    )


# -- per-firm feeds -------------------------------------------------------


def build_state() -> State:
    from ftmo_calendar.state import PostState, TrackedEvent

    def event(key: str, kind: str) -> TrackedEvent:
        return TrackedEvent(
            event_key=key,
            google_event_id=f"g{key}",
            start="2026-07-01T10:00:00+03:00",
            end="2026-07-01T12:00:00+03:00",
            summary=f"event {key}",
            event_type=kind,
        )

    return State(
        posts={
            "f-1": PostState("h", NOW.isoformat(), [event("k1", "maintenance")], firm="ftmo"),
            "t-1": PostState("h", NOW.isoformat(), [event("k2", "early_close")], firm="topstep"),
            "b-1": PostState(
                "h", NOW.isoformat(), [event("k3", "crypto_closure")], firm="blueberry-funded"
            ),
        }
    )


def render(**kwargs) -> str:
    return render_ics(build_state(), (60,), tz_name="Etc/GMT-3", now=NOW, **kwargs)


def test_the_unfiltered_feed_still_contains_everything() -> None:
    ics = render()
    assert ics.count("BEGIN:VEVENT") == 3


def test_a_single_firm_feed_contains_only_that_firm() -> None:
    ics = render(firms=frozenset({"topstep"}))
    assert ics.count("BEGIN:VEVENT") == 1
    assert "UID:k2@" in ics
    assert "UID:k1@" not in ics


def test_a_multi_firm_feed_contains_exactly_those_firms() -> None:
    ics = render(firms=frozenset({"ftmo", "blueberry-funded"}))
    assert ics.count("BEGIN:VEVENT") == 2
    assert "UID:k2@" not in ics


def test_firm_and_type_filters_combine() -> None:
    assert (
        render(firms=frozenset({"topstep"}), types=frozenset({"maintenance"})).count("BEGIN:VEVENT")
        == 0
    )
    assert (
        render(firms=frozenset({"topstep"}), types=frozenset({"early_close"})).count("BEGIN:VEVENT")
        == 1
    )


def test_a_feed_for_a_firm_with_nothing_scheduled_is_valid_and_empty() -> None:
    ics = render(firms=frozenset({"e8-markets"}), firm_titles={"e8-markets": "E8 Markets"})
    assert "BEGIN:VEVENT" not in ics
    assert "BEGIN:VCALENDAR" in ics and "END:VCALENDAR" in ics
    assert "X-WR-CALNAME:E8 Markets Trading Updates" in ics
