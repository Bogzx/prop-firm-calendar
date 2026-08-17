"""Scraper tests, run against pages recorded from the live site.

The fixtures under tests/fixtures/ftmo/ are real FTMO pages captured by
scripts/record_fixtures.py, not markup written to match this code. That
distinction is the whole value: a hand-authored fixture agrees with the scraper
by construction, including when the scraper is wrong.
"""

from datetime import date
from pathlib import Path

import pytest

from ftmo_calendar.sources.ftmo import (
    FtmoSource,
    ScrapeError,
    parse_title_date,
    post_key_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ftmo"
LISTING = (FIXTURES / "listing.html").read_text(encoding="utf-8")
MEMORIAL_DAY_URL = "https://ftmo.com/en/blog/trading-updates/trading-update-21-may-2026/"
MEMORIAL_DAY = (FIXTURES / "trading-update-21-may-2026.html").read_text(encoding="utf-8")
# This one was recorded with its related-posts strip, so it carries the exact
# structure that made the old loose fallback selector dangerous.
WITH_TEASERS_URL = "https://ftmo.com/en/blog/trading-updates/trading-update-6-aug-2026/"
WITH_TEASERS = (FIXTURES / "trading-update-6-aug-2026.html").read_text(encoding="utf-8")


def test_parse_title_date_day_first() -> None:
    assert parse_title_date("Trading Update | 28 May 2026") == date(2026, 5, 28)


def test_parse_title_date_month_first() -> None:
    assert parse_title_date("Trading Update | Jun 4 2026") == date(2026, 6, 4)


def test_parse_title_date_unparseable() -> None:
    assert parse_title_date("Hello world") is None


def test_post_key_is_format_independent() -> None:
    # The same post appears month-first when embedded, day-first on its detail page.
    a = post_key_for("Trading Update | Jun 4 2026", "https://ftmo.com/en/trading-updates/")
    b = post_key_for(
        "Trading Update | 4 Jun 2026",
        "https://ftmo.com/en/blog/trading-updates/trading-update-4-jun-2026/",
    )
    assert a == b == "trading-update-2026-06-04"


def test_post_key_falls_back_to_slug() -> None:
    key = post_key_for("No date here", "https://ftmo.com/en/blog/trading-updates/some-slug/")
    assert key == "some-slug"


def test_parse_listing_extracts_embedded_post_and_links() -> None:
    post, links = FtmoSource().parse_listing(LISTING)
    assert post is not None
    assert post.post_key == "trading-update-2026-08-13"
    assert post.title == "Trading Update | 13 Aug 2026"
    assert "GMT+3" in post.text
    assert len(post.text) > 500  # the whole announcement, not a teaser
    assert links, "the index links to older posts"
    assert all("/blog/trading-updates/" in link for link in links)
    assert all(link.startswith("https://") for link in links)  # relative hrefs resolved


def test_parse_listing_skips_navigation_links() -> None:
    """Only post links are followed — nav, pagination and tracking links are not."""
    _, links = FtmoSource().parse_listing(LISTING)
    assert not any(link.rstrip("/").endswith("/trading-updates") for link in links)


def test_parse_post_extracts_detail_page() -> None:
    post = FtmoSource().parse_post(MEMORIAL_DAY, MEMORIAL_DAY_URL)
    assert post.post_key == "trading-update-2026-05-21"
    assert post.title == "Trading Update | 21 May 2026"
    assert post.url == MEMORIAL_DAY_URL
    assert "Memorial Day" in post.text
    assert "Weekend Maintenance" in post.text


def test_parse_post_raises_on_missing_container() -> None:
    with pytest.raises(ScrapeError):
        FtmoSource().parse_post("<html><body><p>nothing</p></body></html>", "https://x/")


def test_parse_post_refuses_a_related_posts_teaser() -> None:
    """Structural drift must raise, not silently scrape the wrong element.

    The real detail page carries a strip of `article.post-card` teasers. An
    earlier fallback selector — `article, div.entry-content` — matched the
    first of them, so losing `div.content.tu` would have handed the LLM a list
    of headlines and produced confident, wrong calendar entries instead of an
    error. Removing the announcement container must now fail loudly.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(WITH_TEASERS, "html.parser")
    container = soup.select_one("div.content.tu")
    assert container is not None, "fixture must contain the real container"
    container.decompose()

    teaser = soup.select_one("article, div.entry-content")  # the old fallback
    assert teaser is not None, "the real page still offers a teaser to mis-scrape"
    assert "post-card" in (teaser.get("class") or []), "…and it is a related-posts card"

    with pytest.raises(ScrapeError, match="refusing to guess"):
        FtmoSource().parse_post(str(soup), WITH_TEASERS_URL)


def test_parse_post_rejects_a_too_short_container() -> None:
    """A container that exists but holds a stub is drift, not an announcement."""
    html = (
        "<html><body><main><h1>Trading Update | 1 Jan 2026</h1>"
        '<div class="content tu">Coming soon.</div></main></body></html>'
    )
    with pytest.raises(ScrapeError, match="only"):
        FtmoSource().parse_post(html, MEMORIAL_DAY_URL)


def test_fetch_raises_when_the_page_yields_nothing() -> None:
    source = FtmoSource()
    source._get = lambda url: "<html><body><main><p>redesigned</p></main></body></html>"  # type: ignore[method-assign]
    with pytest.raises(ScrapeError, match="page structure may have changed"):
        source.fetch()
