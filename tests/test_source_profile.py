"""Config-driven source profiles: a new prop firm is a TOML file and a fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from ftmo_calendar.config import ConfigError, SourceConfig
from ftmo_calendar.sources.factory import make_source, resolve_source_settings
from ftmo_calendar.sources.profile import (
    PROFILE_DIR,
    available_profiles,
    load_profile,
    profile_from_dict,
)
from ftmo_calendar.sources.web import ScrapeError, WebSource

FIXTURES = Path(__file__).parent / "fixtures" / "ftmo"


def test_ftmo_profile_ships_and_loads() -> None:
    assert "ftmo" in available_profiles()
    profile = load_profile("ftmo")
    assert profile.display_name == "FTMO"
    assert profile.url == "https://ftmo.com/en/trading-updates/"
    assert profile.timezone == "Etc/GMT-3"
    assert "maintenance" in profile.keywords
    assert profile.prompt_hints.strip()


def test_bundled_profiles_are_all_valid() -> None:
    for name in available_profiles():
        assert load_profile(name).url


def test_the_example_template_is_a_working_profile() -> None:
    """The template contributors copy must itself parse."""
    profile = load_profile("example-firm")
    assert profile.post_content_selectors
    assert profile.min_content_chars > 0


def test_unknown_profile_names_the_alternatives() -> None:
    with pytest.raises(ConfigError, match="ftmo"):
        load_profile("no-such-firm")


def test_profile_can_be_loaded_from_an_arbitrary_path(tmp_path: Path) -> None:
    """A firm can live outside the package — no fork required to add one."""
    path = tmp_path / "myfirm.toml"
    path.write_text(
        'url = "https://example.com/news/"\npost_content_selectors = ["div.post"]\n',
        encoding="utf-8",
    )
    profile = load_profile(str(path))
    assert profile.name == "myfirm"
    assert profile.post_content_selectors == ("div.post",)


def test_missing_required_keys_are_rejected() -> None:
    with pytest.raises(ConfigError, match="post_content_selectors"):
        profile_from_dict({"url": "https://x/"}, default_name="x")


def test_a_string_selector_is_accepted_as_one_selector() -> None:
    profile = profile_from_dict(
        {"url": "https://x/", "post_content_selectors": "div.post"}, default_name="x"
    )
    assert profile.post_content_selectors == ("div.post",)


def test_unknown_profile_keys_are_reported_not_ignored() -> None:
    with pytest.raises(ConfigError, match="invalid source profile"):
        profile_from_dict(
            {"url": "https://x/", "post_content_selectors": ["p"], "slectors": []},
            default_name="x",
        )


# -- a second firm, defined entirely in configuration ---------------------

OTHER_FIRM = {
    "name": "otherfirm",
    "display_name": "Other Prop Firm",
    "url": "https://other.example/announcements/",
    "listing_title_selector": "h1",
    "listing_title_contains": "market notice",
    "listing_content_selectors": ["div.notice-body"],
    "link_selectors": ["li.notice"],
    "link_url_contains": "/announcements/",
    "post_content_selectors": ["div.notice-body"],
    "post_key_prefix": "other-notice",
    "min_content_chars": 50,
    "timezone": "Etc/GMT-2",
    "keywords": ["downtime"],
}

OTHER_LISTING = """
<html><body><main>
  <h1>Market Notice | 3 Jun 2026</h1>
  <div class="notice-body">Scheduled downtime on Saturday 6 Jun 2026 from 01:00 to 04:00
  affecting all trading servers. Please close positions beforehand.</div>
  <ul>
    <li class="notice">
      <a href="/announcements/notice-27-may-2026/">Market Notice | 27 May 2026</a>
    </li>
    <li class="notice"><a href="https://other.example/careers/">We are hiring</a></li>
  </ul>
</main></body></html>
"""


def other_source() -> WebSource:
    return WebSource(profile_from_dict(OTHER_FIRM, default_name="otherfirm"))


def test_a_new_firm_needs_no_python() -> None:
    post, links = other_source().parse_listing(OTHER_LISTING)
    assert post is not None
    assert post.post_key == "other-notice-2026-06-03"  # its own key namespace
    assert "Scheduled downtime" in post.text
    # The careers link is filtered out by link_url_contains, and the relative
    # href is resolved against the index URL.
    assert links == ["https://other.example/announcements/notice-27-may-2026/"]


def test_each_firm_keeps_its_own_timezone_and_keywords() -> None:
    profile = profile_from_dict(OTHER_FIRM, default_name="otherfirm")
    assert profile.timezone == "Etc/GMT-2"
    assert profile.keywords == ("downtime",)


def test_profile_defaults_apply_when_source_config_is_untouched() -> None:
    profile, timezone, keywords = resolve_source_settings(SourceConfig())
    assert profile.name == "ftmo"
    assert timezone == "Etc/GMT-3"
    assert keywords == load_profile("ftmo").keywords


def test_explicit_source_settings_beat_the_profile() -> None:
    cfg = SourceConfig(timezone="Europe/Prague", keywords=("wartung",))
    _, timezone, keywords = resolve_source_settings(cfg)
    assert timezone == "Europe/Prague"
    assert keywords == ("wartung",)


def test_make_source_uses_the_configured_profile() -> None:
    source = make_source(SourceConfig(profile="example-firm"))
    assert source.profile.name == "example-firm"
    assert source.url == "https://example.com/trading-announcements/"


def test_source_config_url_overrides_the_profile_url() -> None:
    source = make_source(SourceConfig(url="https://staging.ftmo.com/updates/"))
    assert source.url == "https://staging.ftmo.com/updates/"


# -- drift is an error, never a guess -------------------------------------


def test_a_profile_with_no_matching_selector_raises() -> None:
    with pytest.raises(ScrapeError, match="none of them matched"):
        other_source().parse_post("<html><body><main><p>redesigned</p></main></body></html>", "u")


def test_the_error_names_the_firm_and_the_selectors_tried() -> None:
    with pytest.raises(ScrapeError) as excinfo:
        other_source().parse_post("<html><body></body></html>", "https://other.example/x/")
    message = str(excinfo.value)
    assert "Other Prop Firm" in message
    assert "div.notice-body" in message


def test_profile_dir_is_packaged_next_to_the_code() -> None:
    """Profiles must ship in the wheel, not only in a source checkout."""
    assert PROFILE_DIR.is_dir()
    assert (PROFILE_DIR / "ftmo.toml").is_file()
