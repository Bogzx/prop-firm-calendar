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

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

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
    """GET with a browser user-agent, bounded retries and exponential backoff."""

    def __init__(self, *, timeout: int = 30, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    def get(self, url: str) -> str:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
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
