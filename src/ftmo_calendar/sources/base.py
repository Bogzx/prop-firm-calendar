"""Shared scraping primitives: errors, date parsing, post identity, HTTP retry.

Everything here is firm-agnostic. What differs between prop firms is *where*
on the page the announcement lives, and that is declared in a source profile
(see profile.py) rather than written as Python.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import date

import requests

from ftmo_calendar.sources.politeness import (
    USER_AGENT,
    RateLimiter,
    RobotsDisallowed,
    RobotsPolicy,
)

logger = logging.getLogger(__name__)

__all__ = [
    "USER_AGENT",
    "FetchError",
    "HttpFetcher",
    "RobotsDisallowed",
    "ScrapeError",
    "parse_title_date",
    "post_key_for",
]

_MONTHS = {
    name.lower(): i
    for i, names in enumerate(
        [
            ("Jan", "January"),
            ("Feb", "February"),
            ("Mar", "March"),
            ("Apr", "April"),
            ("May",),
            ("Jun", "June"),
            ("Jul", "July"),
            ("Aug", "August"),
            ("Sep", "September"),
            ("Oct", "October"),
            ("Nov", "November"),
            ("Dec", "December"),
        ],
        start=1,
    )
    for name in names
}

_DAY_FIRST = re.compile(r"(\d{1,2})[\s-]+([A-Za-z]{3,9})[\s-]+(\d{4})")
_MONTH_FIRST = re.compile(r"([A-Za-z]{3,9})[\s-]+(\d{1,2})[\s-]+(\d{4})")


class FetchError(Exception):
    """Network-level failure (transient; retried)."""


class ScrapeError(Exception):
    """Page fetched but the expected structure was missing."""


def parse_title_date(text: str) -> date | None:
    """Parse '28 May 2026', 'Jun 4 2026', or slug '...-28-may-2026' into a date."""
    m = _DAY_FIRST.search(text)
    if m and (month := _MONTHS.get(m.group(2).lower())):
        with contextlib.suppress(ValueError):
            return date(int(m.group(3)), month, int(m.group(1)))
    m = _MONTH_FIRST.search(text)
    if m and (month := _MONTHS.get(m.group(1).lower())):
        with contextlib.suppress(ValueError):
            return date(int(m.group(3)), month, int(m.group(2)))
    return None


def post_key_for(title: str, url: str, prefix: str = "trading-update") -> str:
    """Stable post identity. Prefer the date (title formats vary), else the URL slug."""
    parsed = parse_title_date(title) or parse_title_date(url.rstrip("/").rsplit("/", 1)[-1])
    if parsed:
        return f"{prefix}-{parsed.isoformat()}"
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


class HttpFetcher:
    """Polite GET: honest UA, robots.txt honoured, rate-limited, bounded retries.

    The politeness pieces are constructor-injected and default to shared,
    process-wide instances so that every firm's scraper queues behind the same
    per-host rate limiter and reuses one robots.txt fetch per host.
    """

    def __init__(
        self,
        *,
        timeout: int = 30,
        retries: int = 3,
        user_agent: str = USER_AGENT,
        robots: RobotsPolicy | None = None,
        limiter: RateLimiter | None = None,
        obey_robots: bool = True,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.obey_robots = obey_robots
        self._robots = robots if robots is not None else shared_robots_policy(user_agent)
        self._limiter = limiter if limiter is not None else shared_rate_limiter()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent

    def get(self, url: str) -> str:
        if self.obey_robots and not self._robots.allows(url):
            raise RobotsDisallowed(
                f"robots.txt at {url} disallows {self.user_agent!r}. "
                "Refusing to fetch: this firm cannot be scraped politely, so it must "
                "not be scraped at all."
            )
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._limiter.wait(url, self._robots.crawl_delay(url) if self.obey_robots else None)
            try:
                response = self._session.get(url, timeout=self.timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    raise FetchError(f"HTTP {response.status_code} from {url}")
                response.raise_for_status()
                return response.text
            except (requests.RequestException, FetchError) as e:
                last = e
                logger.warning(
                    "Fetch attempt %d/%d failed for %s: %s", attempt, self.retries, url, e
                )
                if attempt < self.retries:
                    time.sleep(2**attempt)
        raise FetchError(f"could not fetch {url} after {self.retries} attempts: {last}")


_shared_robots: dict[str, RobotsPolicy] = {}
_shared_limiter: RateLimiter | None = None


def shared_robots_policy(user_agent: str = USER_AGENT) -> RobotsPolicy:
    """One robots.txt cache per user-agent, shared by every source in the process."""
    policy = _shared_robots.get(user_agent)
    if policy is None:
        policy = RobotsPolicy(user_agent)
        _shared_robots[user_agent] = policy
    return policy


def shared_rate_limiter() -> RateLimiter:
    """One per-host rate limiter for the whole process.

    Two firms on the same host (a marketing site and its help subdomain are
    different hosts; a firm with two profiles is not) must not each get their
    own budget — the host sees one client, so there is one limiter.
    """
    global _shared_limiter  # noqa: PLW0603 - process-wide singleton by design
    if _shared_limiter is None:
        _shared_limiter = RateLimiter()
    return _shared_limiter
