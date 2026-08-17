"""FTMO source — now a thin binding of the generic scraper to the FTMO profile.

The scraping logic lives in `web.WebSource`, driven by
`sources/profiles/ftmo.toml`. This module stays as the stable import path and
keeps `FtmoSource(url, ...)` working unchanged.

Adding another prop firm no longer means writing a module like this one: write
a profile TOML and a fixture. See sources/profiles/example-firm.toml.
"""

from __future__ import annotations

from ftmo_calendar.sources.base import (
    USER_AGENT,
    FetchError,
    ScrapeError,
    parse_title_date,
    post_key_for,
)
from ftmo_calendar.sources.profile import load_profile
from ftmo_calendar.sources.web import WebSource

__all__ = [
    "USER_AGENT",
    "FetchError",
    "FtmoSource",
    "ScrapeError",
    "parse_title_date",
    "post_key_for",
]


class FtmoSource(WebSource):
    """WebSource bound to the bundled FTMO profile."""

    def __init__(
        self,
        url: str | None = None,
        *,
        max_posts: int = 4,
        max_age_days: int = 14,
        timeout: int = 30,
        retries: int = 3,
    ) -> None:
        super().__init__(
            load_profile("ftmo"),
            url=url,
            max_posts=max_posts,
            max_age_days=max_age_days,
            timeout=timeout,
            retries=retries,
        )
