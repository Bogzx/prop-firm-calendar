"""Rate limiting, robots.txt compliance and request staggering.

One firm on a six-hour interval is a rounding error in anybody's access log.
A dozen firms, all fetched back-to-back the instant the sync loop wakes, is a
small burst against a dozen unrelated hosts at exactly the same second of every
interval — and every one of those hosts is a company that could simply block
the project instead of complaining.

So: identify honestly in the User-Agent, ask robots.txt first and believe the
answer, keep a floor on how often the same host is touched, and jitter the
start of each firm's fetch so the schedule is not a synchronised thundering
herd.

`RobotsPolicy.allows()` fails *open* on a robots.txt that cannot be fetched —
a 500 or a timeout on a robots file is not consent withheld, it is no answer,
and treating every network blip as a prohibition would silently empty the
calendar. A robots.txt that loads and says no is obeyed absolutely.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

#: The token other operators will see in their logs and can match on. An honest
#: UA is what makes "please stop" possible without an IP ban, and what makes a
#: robots.txt rule addressed to us actually reachable.
PROJECT_UA_TOKEN = "TradingCalendarBot"
USER_AGENT = (
    f"Mozilla/5.0 (compatible; {PROJECT_UA_TOKEN}/1.0; +https://github.com/Bogzx/ftmo-calendar)"
)

#: Floor between two requests to the same host, unless robots.txt asks for more.
DEFAULT_MIN_INTERVAL_SECONDS = 2.0
#: Upper bound on a Crawl-delay we will actually honour. A site asking for an
#: hour between requests is asking us not to scrape it on any useful schedule;
#: that is a decision for a human, not a sleep() that stalls the sync loop.
MAX_HONOURED_CRAWL_DELAY = 30.0
#: Random delay added before each firm's first request, so N firms configured on
#: the same interval do not all fire on the same second.
DEFAULT_STAGGER_SECONDS = 5.0


class RobotsDisallowed(Exception):
    """robots.txt explicitly forbids this URL for our user-agent.

    Raised rather than logged-and-skipped: a firm we are not allowed to scrape
    must be visibly absent, not quietly missing from the calendar.
    """


def _host_root(url: str) -> str:
    parts = urlparse(url)
    return urlunparse((parts.scheme, parts.netloc, "/robots.txt", "", "", ""))


def _host_key(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class _HostRules:
    parser: urllib.robotparser.RobotFileParser | None
    crawl_delay: float | None


class RobotsPolicy:
    """Caches one robots.txt per host and answers allow/deny plus crawl delay."""

    def __init__(self, user_agent: str = USER_AGENT, *, timeout: int = 15) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, _HostRules] = {}
        self._lock = threading.Lock()

    def _rules(self, url: str) -> _HostRules:
        key = _host_key(url)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        rules = self._fetch(url)
        with self._lock:
            self._cache[key] = rules
        return rules

    def _fetch(self, url: str) -> _HostRules:
        robots_url = _host_root(url)
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            # requests, not parser.read(): urllib sends a Python-urllib UA that
            # a good many WAFs answer with a challenge page, which then parses
            # as an empty (allow-everything) robots file — the wrong answer
            # arrived at politely.
            import requests

            response = requests.get(
                robots_url, timeout=self.timeout, headers={"User-Agent": self.user_agent}
            )
            if response.status_code >= 400:
                # RFC 9309: 4xx means "no restrictions"; 5xx should mean "assume
                # disallowed", but treating a flaky origin as a permanent ban
                # would empty subscribers' calendars over someone else's outage.
                logger.info(
                    "robots.txt for %s returned HTTP %d; proceeding without rules",
                    _host_key(url),
                    response.status_code,
                )
                return _HostRules(parser=None, crawl_delay=None)
            parser.parse(response.text.splitlines())
        except Exception as e:  # noqa: BLE001 - never let robots fetching break a sync
            logger.info("Could not read %s (%s); proceeding without rules", robots_url, e)
            return _HostRules(parser=None, crawl_delay=None)

        delay: float | None = None
        for agent in (self.user_agent, PROJECT_UA_TOKEN, "*"):
            raw = parser.crawl_delay(agent)
            if raw is not None:
                delay = float(raw)
                break
        return _HostRules(parser=parser, crawl_delay=delay)

    def allows(self, url: str) -> bool:
        """True unless a readable robots.txt disallows this URL for us."""
        rules = self._rules(url)
        if rules.parser is None:
            return True
        # Check the full UA string and the bare token: operators write rules
        # against either, and matching is a substring test on the token.
        return bool(
            rules.parser.can_fetch(self.user_agent, url)
            and rules.parser.can_fetch(PROJECT_UA_TOKEN, url)
        )

    def crawl_delay(self, url: str) -> float | None:
        delay = self._rules(url).crawl_delay
        if delay is None:
            return None
        if delay > MAX_HONOURED_CRAWL_DELAY:
            logger.warning(
                "robots.txt for %s asks for a %.0fs crawl delay; capping at %.0fs",
                _host_key(url),
                delay,
                MAX_HONOURED_CRAWL_DELAY,
            )
            return MAX_HONOURED_CRAWL_DELAY
        return delay


@dataclass
class RateLimiter:
    """Enforces a minimum gap between requests to the same host, process-wide."""

    min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS
    sleep: object = field(default=time.sleep, repr=False)
    clock: object = field(default=time.monotonic, repr=False)
    _last: dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def wait(self, url: str, extra_interval: float | None = None) -> float:
        """Block until this host may be hit again; returns the seconds waited."""
        interval = max(self.min_interval, extra_interval or 0.0)
        key = _host_key(url)
        now: float = self.clock()  # type: ignore[operator]
        with self._lock:
            previous = self._last.get(key)
            due = now if previous is None else previous + interval
            wait_for = max(0.0, due - now)
            # Reserve the slot before sleeping so concurrent callers queue up
            # behind each other instead of all measuring the same free slot.
            self._last[key] = max(now, due)
        if wait_for > 0:
            logger.debug("Rate limit: waiting %.2fs before %s", wait_for, key)
            self.sleep(wait_for)  # type: ignore[operator]
        return wait_for


def stagger(max_seconds: float = DEFAULT_STAGGER_SECONDS, sleep=time.sleep) -> float:  # noqa: ANN001
    """Sleep a random fraction of `max_seconds`; returns the delay used.

    Called once per firm per run so that N firms sharing one sync interval
    spread their first request out instead of arriving together.
    """
    if max_seconds <= 0:
        return 0.0
    delay = random.uniform(0, max_seconds)  # noqa: S311 - jitter, not cryptography
    sleep(delay)
    return delay
