"""Scraping politeness: robots.txt, per-host rate limiting, stagger, honest UA.

Scraping one firm on a six-hour interval is invisible. Scraping a dozen is a
recurring burst against a dozen companies who can block the project instead of
complaining — so these rules are a condition of the expansion, not decoration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prop_firm_calendar.sources.base import HttpFetcher, shared_rate_limiter, shared_robots_policy
from prop_firm_calendar.sources.politeness import (
    MAX_HONOURED_CRAWL_DELAY,
    PROJECT_UA_TOKEN,
    USER_AGENT,
    RateLimiter,
    RobotsDisallowed,
    RobotsPolicy,
    stagger,
)

ALLOW_ALL = "User-agent: *\nDisallow:\n"
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
DISALLOW_US = f"User-agent: {PROJECT_UA_TOKEN}\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
CRAWL_DELAY = "User-agent: *\nAllow: /\nCrawl-delay: 4\n"


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


@pytest.fixture
def fake_robots(monkeypatch: pytest.MonkeyPatch):
    """Serve a canned robots.txt to RobotsPolicy without touching the network."""
    served: dict[str, FakeResponse] = {}

    def fake_get(url, timeout=None, headers=None):  # noqa: ANN001, ARG001
        return served.get(url, FakeResponse("", 404))

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    return served


# -- the user agent -------------------------------------------------------


def test_user_agent_identifies_the_project_and_how_to_reach_it() -> None:
    """An operator who wants us to stop must be able to find out who we are."""
    assert PROJECT_UA_TOKEN in USER_AGENT
    assert "github.com/Bogzx/prop-firm-calendar" in USER_AGENT
    assert "Chrome" not in USER_AGENT, "no longer impersonating a browser"


def test_fetcher_sends_the_identifying_user_agent() -> None:
    fetcher = HttpFetcher(obey_robots=False)
    assert fetcher._session.headers["User-Agent"] == USER_AGENT


# -- robots.txt -----------------------------------------------------------


def test_a_disallowed_path_is_refused(fake_robots) -> None:
    fake_robots["https://blocked.example/robots.txt"] = FakeResponse(DISALLOW_ALL)
    policy = RobotsPolicy()
    assert policy.allows("https://blocked.example/news/") is False


def test_a_rule_naming_this_bot_specifically_is_obeyed(fake_robots) -> None:
    """A site that allows everyone else but bans us is still banning us."""
    fake_robots["https://picky.example/robots.txt"] = FakeResponse(DISALLOW_US)
    assert RobotsPolicy().allows("https://picky.example/news/") is False


def test_an_allowing_site_is_allowed(fake_robots) -> None:
    fake_robots["https://open.example/robots.txt"] = FakeResponse(ALLOW_ALL)
    assert RobotsPolicy().allows("https://open.example/news/") is True


def test_a_missing_robots_txt_is_not_a_prohibition(fake_robots) -> None:
    """RFC 9309: 4xx means no restrictions. Absence of an answer is not a no."""
    assert RobotsPolicy().allows("https://silent.example/news/") is True


def test_an_unreachable_robots_txt_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Someone else's outage must not silently empty subscribers' calendars."""
    import requests

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    assert RobotsPolicy().allows("https://flaky.example/news/") is True


def test_robots_is_fetched_once_per_host(fake_robots, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    fake_robots["https://cached.example/robots.txt"] = FakeResponse(ALLOW_ALL)
    import requests

    inner = requests.get

    def counting(url, **kwargs):  # noqa: ANN001, ANN003
        calls.append(url)
        return inner(url, **kwargs)

    monkeypatch.setattr(requests, "get", counting)
    policy = RobotsPolicy()
    for _ in range(5):
        policy.allows("https://cached.example/a")
        policy.crawl_delay("https://cached.example/b")
    assert len(calls) == 1


def test_crawl_delay_is_read_and_honoured(fake_robots) -> None:
    fake_robots["https://slow.example/robots.txt"] = FakeResponse(CRAWL_DELAY)
    assert RobotsPolicy().crawl_delay("https://slow.example/news/") == 4.0


def test_an_absurd_crawl_delay_is_capped(fake_robots) -> None:
    """An hour between requests is a human decision, not a sleep in the loop."""
    fake_robots["https://glacial.example/robots.txt"] = FakeResponse(
        "User-agent: *\nCrawl-delay: 3600\n"
    )
    assert RobotsPolicy().crawl_delay("https://glacial.example/x") == MAX_HONOURED_CRAWL_DELAY


def test_the_fetcher_refuses_a_disallowed_url(fake_robots) -> None:
    """Loudly, so a firm we may not scrape is visibly absent rather than empty."""
    fake_robots["https://blocked.example/robots.txt"] = FakeResponse(DISALLOW_ALL)
    fetcher = HttpFetcher(robots=RobotsPolicy(), limiter=RateLimiter(min_interval=0))
    with pytest.raises(RobotsDisallowed, match="disallows"):
        fetcher.get("https://blocked.example/news/")


# -- rate limiting --------------------------------------------------------


def test_requests_to_one_host_are_spaced_out() -> None:
    slept: list[float] = []
    now = [1000.0]
    limiter = RateLimiter(min_interval=2.0, sleep=slept.append, clock=lambda: now[0])
    assert limiter.wait("https://a.example/1") == 0.0  # first one is free
    assert limiter.wait("https://a.example/2") == 2.0
    assert slept == [2.0]


def test_different_hosts_do_not_share_a_budget() -> None:
    slept: list[float] = []
    limiter = RateLimiter(min_interval=2.0, sleep=slept.append, clock=lambda: 500.0)
    limiter.wait("https://a.example/1")
    assert limiter.wait("https://b.example/1") == 0.0
    assert slept == []


def test_a_crawl_delay_longer_than_the_floor_wins() -> None:
    slept: list[float] = []
    limiter = RateLimiter(min_interval=1.0, sleep=slept.append, clock=lambda: 0.0)
    limiter.wait("https://a.example/1")
    assert limiter.wait("https://a.example/2", extra_interval=9.0) == 9.0


def test_the_limiter_is_shared_process_wide() -> None:
    """Two firms on one host are one client as far as that host is concerned."""
    assert shared_rate_limiter() is shared_rate_limiter()
    assert shared_robots_policy() is shared_robots_policy()


# -- stagger --------------------------------------------------------------


def test_stagger_stays_within_its_bound() -> None:
    slept: list[float] = []
    for _ in range(50):
        delay = stagger(5.0, sleep=slept.append)
        assert 0.0 <= delay <= 5.0
    assert len(slept) == 50


def test_stagger_can_be_switched_off() -> None:
    slept: list[float] = []
    assert stagger(0, sleep=slept.append) == 0.0
    assert slept == []


class _Response:
    def __init__(self, code: int, text: str = "ok") -> None:
        self.status_code = code
        self.text = text


def _fetcher_answering(codes: list[int], monkeypatch: pytest.MonkeyPatch):
    import prop_firm_calendar.sources.base as base

    monkeypatch.setattr(base.time, "sleep", lambda seconds: None)
    fetcher = HttpFetcher(obey_robots=False, limiter=RateLimiter(min_interval=0), retries=3)
    calls: list[int] = []

    def get(url: str, timeout: int):
        calls.append(codes[len(calls)])
        return _Response(calls[-1])

    monkeypatch.setattr(fetcher._session, "get", get)
    return fetcher, calls


def test_a_404_is_final_and_is_a_scrape_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a missing page three times only repeats a refused request."""
    from prop_firm_calendar.sources.base import PageGone, ScrapeError

    fetcher, calls = _fetcher_answering([404, 200, 200], monkeypatch)
    with pytest.raises(PageGone) as excinfo:
        fetcher.get("https://x.test/gone")
    assert isinstance(excinfo.value, ScrapeError)
    assert calls == [404]


def test_other_client_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    from prop_firm_calendar.sources.base import FetchError, PageGone

    fetcher, calls = _fetcher_answering([403, 200], monkeypatch)
    with pytest.raises(FetchError) as excinfo:
        fetcher.get("https://x.test/forbidden")
    assert not isinstance(excinfo.value, PageGone)
    assert calls == [403]


def test_rate_limits_and_server_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher, calls = _fetcher_answering([429, 503, 200], monkeypatch)
    assert fetcher.get("https://x.test/busy") == "ok"
    assert calls == [429, 503, 200]


def test_a_deleted_linked_post_is_skipped_not_fatal() -> None:
    from prop_firm_calendar.sources.base import PageGone
    from prop_firm_calendar.sources.ftmo import FtmoSource

    listing = (Path(__file__).parent / "fixtures" / "ftmo" / "listing.html").read_text(
        encoding="utf-8"
    )

    requested: list[str] = []

    class Fetcher:
        def get(self, url: str) -> str:
            requested.append(url)
            if url == "https://ftmo.com/en/trading-updates/":
                return listing
            raise PageGone(f"HTTP 404 from {url}")

    source = FtmoSource(max_age_days=100_000)  # every linked post is in range
    source._fetcher = Fetcher()  # type: ignore[assignment]
    posts = source.fetch()
    assert len(requested) > 1, "the linked posts must actually have been tried"
    assert len(posts) == 1  # the embedded post survives the dead links
