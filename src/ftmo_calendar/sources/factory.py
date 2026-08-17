"""Construct configured sources from `[[firms]]` (or legacy `[source]`) plus profiles."""

from __future__ import annotations

from dataclasses import dataclass

from ftmo_calendar.config import FirmConfig, ScrapeConfig, SourceConfig
from ftmo_calendar.sources.base import HttpFetcher, shared_rate_limiter, shared_robots_policy
from ftmo_calendar.sources.politeness import USER_AGENT
from ftmo_calendar.sources.profile import SourceProfile, load_profile
from ftmo_calendar.sources.web import WebSource


@dataclass(frozen=True)
class ResolvedFirm:
    """A firm ready to run: its profile, its scraper, and its effective settings.

    `timezone` and `keywords` are resolved here rather than at use sites so
    there is exactly one place that decides whether the profile's value or the
    user's override wins — getting that wrong per-firm is how a firm ends up
    published an hour off.
    """

    profile: SourceProfile
    source: WebSource
    timezone: str
    keywords: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def display_name(self) -> str:
        return self.profile.display_name


def make_source(cfg: SourceConfig) -> WebSource:
    """Build the scraper for the legacy single `[source]` section.

    Explicit `[source]` settings win over the profile's defaults so an existing
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
    """Return (profile, timezone, keywords) with `[source]` overriding the profile.

    A user who never touched `[source]` gets the profile's own timezone and
    keywords — which is what makes a firm a TOML file. A user who set them
    keeps their values.
    """
    profile = load_profile(cfg.profile)
    defaults = SourceConfig()
    timezone = cfg.timezone if cfg.timezone != defaults.timezone else profile.timezone
    keywords = cfg.keywords if cfg.keywords != defaults.keywords else profile.keywords
    return profile, timezone or defaults.timezone, keywords or defaults.keywords


def make_fetcher(scrape: ScrapeConfig | None = None) -> HttpFetcher:
    """One politely-configured fetcher; hosts are rate-limited process-wide."""
    scrape = scrape or ScrapeConfig()
    user_agent = scrape.user_agent or USER_AGENT
    limiter = shared_rate_limiter()
    limiter.min_interval = max(limiter.min_interval, scrape.min_request_interval_seconds)
    return HttpFetcher(
        user_agent=user_agent,
        robots=shared_robots_policy(user_agent),
        limiter=limiter,
        obey_robots=scrape.obey_robots,
    )


def resolve_firm(firm: FirmConfig, scrape: ScrapeConfig | None = None) -> ResolvedFirm:
    """Turn one `[[firms]]` entry into a runnable source with settled settings."""
    profile = load_profile(firm.profile)
    defaults = SourceConfig()
    timezone = firm.timezone or profile.timezone or defaults.timezone
    keywords = firm.keywords if firm.keywords is not None else profile.keywords
    source = WebSource(
        profile,
        url=firm.url or profile.url,
        max_posts=firm.max_posts,
        max_age_days=firm.max_age_days,
        fetcher=make_fetcher(scrape),
    )
    return ResolvedFirm(
        profile=profile,
        source=source,
        timezone=timezone,
        keywords=keywords or defaults.keywords,
    )
