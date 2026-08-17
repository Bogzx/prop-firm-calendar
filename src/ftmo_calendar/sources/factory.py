"""Construct the configured source from [source] plus its profile."""

from __future__ import annotations

from ftmo_calendar.config import SourceConfig
from ftmo_calendar.sources.profile import SourceProfile, load_profile
from ftmo_calendar.sources.web import WebSource


def make_source(cfg: SourceConfig) -> WebSource:
    """Build the scraper for the configured profile.

    Explicit [source] settings win over the profile's defaults so an existing
    config.toml keeps behaving exactly as it did. `url` needs the same
    untouched-means-profile treatment as the rest: its dataclass default is
    FTMO's index page, which would otherwise be forced onto every other firm.
    """
    profile = load_profile(cfg.profile)
    url = cfg.url if cfg.url != SourceConfig().url else profile.url
    return WebSource(
        profile,
        url=url or profile.url,
        max_posts=cfg.max_posts,
        max_age_days=cfg.max_age_days,
    )


def resolve_source_settings(cfg: SourceConfig) -> tuple[SourceProfile, str, tuple[str, ...]]:
    """Return (profile, timezone, keywords) with [source] overriding the profile.

    A user who never touched [source] gets the profile's own timezone and
    keywords — which is what makes a firm a TOML file. A user who set them
    keeps their values.
    """
    profile = load_profile(cfg.profile)
    defaults = SourceConfig()
    timezone = cfg.timezone if cfg.timezone != defaults.timezone else profile.timezone
    keywords = cfg.keywords if cfg.keywords != defaults.keywords else profile.keywords
    return profile, timezone or defaults.timezone, keywords or defaults.keywords
