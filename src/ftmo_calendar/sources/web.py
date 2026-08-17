"""Generic announcements scraper driven by a SourceProfile.

Holds no knowledge of any particular firm: give it a profile and it fetches the
index page, reads the embedded newest post, follows recent links, and returns
SourcePosts. Structural drift raises ScrapeError instead of returning whatever
happens to be on the page — a wrong announcement body reaches the LLM as
confident prose and comes back as plausible, wrong calendar entries.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ftmo_calendar.models import SourcePost
from ftmo_calendar.sources.base import (
    HttpFetcher,
    ScrapeError,
    parse_title_date,
    post_key_for,
)
from ftmo_calendar.sources.profile import SourceProfile

logger = logging.getLogger(__name__)


class WebSource:
    """Fetches and parses announcement posts for one configured firm."""

    def __init__(
        self,
        profile: SourceProfile,
        *,
        url: str | None = None,
        max_posts: int = 4,
        max_age_days: int = 14,
        timeout: int = 30,
        retries: int = 3,
    ) -> None:
        self.profile = profile
        self.url = url or profile.url
        self.max_posts = max_posts
        self.max_age_days = max_age_days
        self._fetcher = HttpFetcher(timeout=timeout, retries=retries)

    # -- public API -----------------------------------------------------

    def fetch(self) -> list[SourcePost]:
        """Return the embedded latest post plus recent linked posts, newest first."""
        embedded, links = self.parse_listing(self._get(self.url))
        posts: list[SourcePost] = [embedded] if embedded else []
        cutoff = date.today() - timedelta(days=self.max_age_days)
        for link in links:
            if len(posts) >= self.max_posts:
                break
            link_date = parse_title_date(link.rstrip("/").rsplit("/", 1)[-1])
            if link_date and link_date < cutoff:
                continue
            try:
                post = self.parse_post(self._get(link), link)
            except ScrapeError as e:
                logger.warning("Skipping post %s: %s", link, e)
                continue
            if embedded and post.post_key == embedded.post_key:
                continue
            posts.append(post)
        if not posts:
            raise ScrapeError(
                f"No {self.profile.display_name} announcement posts found at {self.url} — "
                "the page structure may have changed"
            )
        return posts

    def parse_listing(self, html: str) -> tuple[SourcePost | None, list[str]]:
        soup = BeautifulSoup(html, "html.parser")
        embedded = self._embedded_post(soup)
        if embedded is None:
            logger.warning("No embedded post found on the listing page %s", self.url)

        links: list[str] = []
        for selector in self.profile.link_selectors:
            for card in soup.select(selector):
                a = card.find("a", href=True) if card.name != "a" else card
                if not isinstance(a, Tag):
                    continue
                href = urljoin(self.url, str(a["href"]))
                if self.profile.link_url_contains and self.profile.link_url_contains not in href:
                    continue
                if href not in links:
                    links.append(href)
        return embedded, links

    def parse_post(self, html: str, url: str) -> SourcePost:
        soup = BeautifulSoup(html, "html.parser")
        text = self._content_text(soup, self.profile.post_content_selectors, url)
        title_node = soup.select_one(self.profile.post_title_selector)
        title = title_node.get_text(" ", strip=True) if title_node else url
        return SourcePost(
            post_key=post_key_for(title, url, self.profile.post_key_prefix),
            title=title,
            text=text,
            url=url,
        )

    # -- internals ------------------------------------------------------

    def _embedded_post(self, soup: BeautifulSoup) -> SourcePost | None:
        if not self.profile.listing_content_selectors:
            return None
        title_node = self._listing_title(soup)
        if title_node is None:
            return None
        try:
            text = self._content_text(soup, self.profile.listing_content_selectors, self.url)
        except ScrapeError as e:
            logger.warning("Listing page has a title but no readable content: %s", e)
            return None
        title = title_node.get_text(" ", strip=True)
        return SourcePost(
            post_key=post_key_for(title, self.url, self.profile.post_key_prefix),
            title=title,
            text=text,
            url=self.url,
        )

    def _listing_title(self, soup: BeautifulSoup) -> Tag | None:
        needle = self.profile.listing_title_contains.lower()
        candidates = soup.select(self.profile.listing_title_selector)
        if not needle:
            return candidates[0] if candidates else None
        return next((h for h in candidates if needle in h.get_text().lower()), None)

    def _content_text(self, soup: BeautifulSoup, selectors: tuple[str, ...], url: str) -> str:
        """First selector that yields a plausible announcement body, else raise.

        The length floor is what turns "the page changed" into an error rather
        than a wrong calendar entry: a nav wrapper or a related-posts teaser
        matches a loose selector happily but never carries an announcement's
        worth of prose.
        """
        too_short: list[str] = []
        for selector in selectors:
            node = soup.select_one(selector)
            if node is None:
                continue
            text = node.get_text(" ", strip=True)
            if len(text) >= self.profile.min_content_chars:
                return text
            too_short.append(f"{selector!r} matched but held only {len(text)} chars")
        detail = "; ".join(too_short) if too_short else "none of them matched"
        raise ScrapeError(
            f"no announcement content at {url}: tried {list(selectors)} — {detail}. "
            f"The {self.profile.display_name} page structure has probably changed; "
            "refusing to guess (a wrong container becomes wrong calendar entries)."
        )

    def _get(self, url: str) -> str:
        return self._fetcher.get(url)
