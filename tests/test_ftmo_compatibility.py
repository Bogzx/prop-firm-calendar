"""Existing FTMO subscribers must not notice that this became multi-firm.

There are real people subscribed to the public feed right now, and two ways to
hurt them. One is the calendar reconcile: `event_key` is the identity Google
Calendar entries are matched on, so if the way it is computed moves at all,
every event already in someone's calendar is orphaned and silently recreated —
duplicated for anyone who kept the old one, and stripped of any reminder they
had personally set. The other is the feed itself: the same state must keep
producing the same ICS bytes, or apps re-sync every event as new.

The hashes below were captured by running the *base branch* (audit/fixes-and-
features, the commit this work started from) against the same fixture, then
diffed against this branch: identical, including the whole-file SHA-256. They
are pinned here as literals so that a future change which alters them has to
change this file too, in a diff a reviewer will see.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ftmo_calendar.config import FTMO_PLATFORM_TZ, EventRules, SourceConfig, load_config
from ftmo_calendar.parsing.llm import RawEvent
from ftmo_calendar.parsing.validate import validate_events
from ftmo_calendar.sinks.ics import render_ics
from ftmo_calendar.sources.factory import resolve_firm
from ftmo_calendar.sources.ftmo import FtmoSource
from ftmo_calendar.state import PostState, State, TrackedEvent, load_state, save_state

FIXTURES = Path(__file__).parent / "fixtures" / "ftmo"
URL = "https://ftmo.com/en/blog/trading-updates/trading-update-21-may-2026/"
TZ = ZoneInfo(FTMO_PLATFORM_TZ)
NOW = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)

POST = FtmoSource().parse_post(
    (FIXTURES / "trading-update-21-may-2026.html").read_text(encoding="utf-8"), URL
)
GOLDEN = json.loads(
    (FIXTURES / "trading-update-21-may-2026.expected.json").read_text(encoding="utf-8")
)
RAW_EVENTS = [RawEvent(**e) for e in GOLDEN["events"]]

# Captured from the base branch. Order matters: it is the order events reach
# the sink, and therefore the order they appear in the feed.
BASE_EVENT_KEYS = [
    "7bcac205b0c2e49e",
    "f78c0a78573e444f",
    "4560b9cb2193c0f3",
    "32ff064208260e58",
    "ca379f46478cb5fd",
    "178472258e3f1561",
    "f7dc26046739e6c0",
    "1b03891222502f07",
]
BASE_POST_KEY = "trading-update-2026-05-21"
BASE_CONTENT_HASH = "ff8a9aa853546e3d1a5e765342dfadb8b737b66e1720e681f436c625fd9f8ffa"
BASE_ICS_SHA256 = "7e5402e7b1b8a7509a1a91e567e2fa22c8f1dcf0b9bf565e4d0152a149841485"


def _events():
    events, rejections = validate_events(RAW_EVENTS, POST, EventRules(), TZ, TZ, now=NOW)
    assert rejections == []
    return events


def _state() -> State:
    return State(
        posts={
            POST.post_key: PostState(
                content_hash=POST.content_hash,
                last_seen=NOW.isoformat(),
                events=[
                    TrackedEvent(
                        event_key=e.event_key,
                        google_event_id=f"g-{i}",
                        end=e.end.isoformat(),
                        summary=e.summary,
                        start=e.start.isoformat(),
                        event_type=e.event_type.value,
                    )
                    for i, e in enumerate(_events())
                ],
            )
        }
    )


def _render(state: State, **kwargs) -> str:
    return render_ics(
        state,
        (60, 10),
        source_url="https://ftmo.com/en/trading-updates/",
        refresh_minutes=360,
        tz_name=FTMO_PLATFORM_TZ,
        now=NOW,
        **kwargs,
    )


# -- reconcile identity ---------------------------------------------------


def test_post_key_is_unchanged() -> None:
    """State is keyed on this; a change makes every tracked post look brand new."""
    assert POST.post_key == BASE_POST_KEY


def test_content_hash_is_unchanged() -> None:
    """A moved hash re-extracts every post and re-bills every LLM call."""
    assert POST.content_hash == BASE_CONTENT_HASH


def test_event_keys_are_byte_for_byte_what_the_base_branch_produced() -> None:
    """The reconcile identity. Orphaning these duplicates real calendars."""
    assert [e.event_key for e in _events()] == BASE_EVENT_KEYS


def test_firm_attribution_is_not_part_of_the_event_key() -> None:
    """Per-firm tracking had to be added *outside* the hash.

    `firm` lives on PostState, never in TradingEvent.event_key. If it ever
    leaked in, every pre-existing event would rekey at once.
    """
    from ftmo_calendar.models import TradingEvent

    fields = {f for f in TradingEvent.__dataclass_fields__}
    assert "firm" not in fields
    event = _events()[0]
    raw = "|".join(
        (
            event.source_post_key,
            event.event_type.value,
            event.start.isoformat(),
            event.end.isoformat(),
        )
    )
    assert event.event_key == hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# -- the feed people are subscribed to ------------------------------------


def test_unfiltered_feed_is_byte_identical_to_the_base_branch() -> None:
    digest = hashlib.sha256(_render(_state()).encode("utf-8")).hexdigest()
    assert digest == BASE_ICS_SHA256


def test_feed_keeps_its_uids_and_calendar_name() -> None:
    ics = _render(_state())
    assert "X-WR-CALNAME:FTMO Trading Updates" in ics
    assert "PRODID:-//AutoFtmoCalendar//ftmo-calendar//EN" in ics
    for key in BASE_EVENT_KEYS:
        assert f"UID:{key}@ftmo-calendar" in ics


def test_an_ftmo_only_feed_is_still_named_after_ftmo() -> None:
    """Naming follows content, so a single-firm deployment reads as it always did."""
    state = _state()
    state.posts[POST.post_key].firm = "ftmo"
    assert "X-WR-CALNAME:FTMO Trading Updates" in _render(state, firm_titles={"ftmo": "FTMO"})


# -- upgrading an existing deployment -------------------------------------


def test_a_pre_multifirm_state_file_loads_and_keeps_every_event(tmp_path: Path) -> None:
    """v3 state has no `firm` key at all; nothing may be dropped on upgrade."""
    path = tmp_path / "state.json"
    save_state(_state(), path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for post in payload["posts"].values():
        post.pop("firm", None)  # exactly what a v3 file looks like
    payload["version"] = 3
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    reloaded = load_state(path)
    assert reloaded.posts[POST.post_key].firm == ""
    assert len(reloaded.posts[POST.post_key].events) == 8
    digest = hashlib.sha256(_render(reloaded).encode("utf-8")).hexdigest()
    assert digest == BASE_ICS_SHA256


def test_legacy_state_still_appears_in_a_per_firm_feed(tmp_path: Path) -> None:
    """Unattributed posts belong to the first configured firm, not to nobody.

    Without this, the day multi-firm ships every existing event would vanish
    from `?firms=ftmo` until its post happened to be re-scraped.
    """
    state = _state()  # firm == "" throughout, as on a freshly upgraded deployment
    filtered = _render(state, firms=frozenset({"ftmo"}), default_firm="ftmo")
    for key in BASE_EVENT_KEYS:
        assert f"UID:{key}@ftmo-calendar" in filtered

    # And it must not leak into another firm's feed.
    other = _render(state, firms=frozenset({"topstep"}), default_firm="ftmo")
    assert "BEGIN:VEVENT" not in other


# -- configuration --------------------------------------------------------


def test_a_config_with_no_firms_section_is_exactly_one_ftmo_firm(tmp_path: Path) -> None:
    """The upgrade path for every config.toml that exists today."""
    path = tmp_path / "config.toml"
    path.write_text("[source]\nprofile = 'ftmo'\n", encoding="utf-8")
    cfg = load_config(path, env={})
    assert len(cfg.firms) == 1
    firm = cfg.firms[0]
    assert firm.profile == "ftmo"
    # Nothing was overridden, so the profile's own settings apply — the same
    # ones resolve_source_settings gave the single source before.
    assert firm.timezone is None and firm.keywords is None and firm.url is None

    resolved = resolve_firm(firm, cfg.scrape)
    assert resolved.timezone == FTMO_PLATFORM_TZ
    assert resolved.keywords == SourceConfig().keywords
    assert resolved.source.url == "https://ftmo.com/en/trading-updates/"
    assert resolved.profile.post_key_prefix == "trading-update"
    assert resolved.profile.require_stated_offset is False


def test_an_empty_config_still_defaults_to_ftmo(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    cfg = load_config(path, env={})
    assert [f.profile for f in cfg.firms] == ["ftmo"]


def test_explicit_source_overrides_still_win(tmp_path: Path) -> None:
    """Someone who tuned [source] by hand keeps their values."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[source]\nprofile = 'ftmo'\ntimezone = 'Europe/Prague'\nkeywords = ['downtime']\n",
        encoding="utf-8",
    )
    cfg = load_config(path, env={})
    resolved = resolve_firm(cfg.firms[0], cfg.scrape)
    assert resolved.timezone == "Europe/Prague"
    assert resolved.keywords == ("downtime",)


def test_shipped_profiles_do_not_collide_on_post_keys() -> None:
    """Post keys share one namespace across firms; a clash would merge two firms.

    FTMO's prefix cannot change (it is baked into live state), so the check is
    that every other shipped profile stays clear of it.
    """
    from ftmo_calendar.sources.profile import available_profiles, load_profile

    prefixes = [load_profile(n).post_key_prefix for n in available_profiles()]
    assert len(prefixes) == len(set(prefixes)), f"duplicate post_key_prefix among {prefixes}"
    assert load_profile("ftmo").post_key_prefix == "trading-update"
