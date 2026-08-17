#!/usr/bin/env python3
"""Record real announcement pages as test fixtures.

CONTRIBUTING promises the parse tests run against *recorded* HTML. This is what
records them, so refreshing a fixture after a site redesign is one command
rather than an afternoon of hand-authoring markup that matches the code (which
is how fixtures end up agreeing with a scraper that is already broken).

    python scripts/record_fixtures.py                 # refresh the FTMO fixtures
    python scripts/record_fixtures.py --profile ftmo --posts 2

What is saved: the page's <main> element, with <script>, <style>, <svg>,
<noscript> and comments removed. Everything the scrapers look at — element
nesting, class names, sibling teaser cards, the announcement prose — is kept
byte-for-byte; only inert weight is dropped, taking a 275 KB page to ~10 KB.
A provenance comment recording the URL and date is prepended.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bs4 import BeautifulSoup, Comment  # noqa: E402

from ftmo_calendar.sources.base import HttpFetcher  # noqa: E402
from ftmo_calendar.sources.profile import load_profile  # noqa: E402
from ftmo_calendar.sources.web import WebSource  # noqa: E402

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"
STRIP_TAGS = ("script", "style", "svg", "noscript")


def trim(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(STRIP_TAGS)):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    main = soup.find("main") or soup.find("body") or soup
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return (
        f"<!-- Recorded from {url} on {stamp} by scripts/record_fixtures.py.\n"
        f"     Verbatim <main> element; {', '.join(STRIP_TAGS)} and comments removed. -->\n"
        f"<html><body>\n{main}\n</body></html>\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="ftmo", help="source profile (default: ftmo)")
    parser.add_argument("--posts", type=int, default=1, help="how many linked posts to save")
    args = parser.parse_args()

    profile = load_profile(args.profile)
    source = WebSource(profile)
    fetcher = HttpFetcher()
    out_dir = FIXTURE_ROOT / profile.name
    out_dir.mkdir(parents=True, exist_ok=True)

    listing_html = fetcher.get(source.url)
    (out_dir / "listing.html").write_text(trim(listing_html, source.url), encoding="utf-8")
    print(f"recorded {out_dir / 'listing.html'} <- {source.url}")

    _, links = source.parse_listing(listing_html)
    for link in links[: args.posts]:
        slug = link.rstrip("/").rsplit("/", 1)[-1]
        page = fetcher.get(link)
        (out_dir / f"{slug}.html").write_text(trim(page, link), encoding="utf-8")
        print(f"recorded {out_dir / (slug + '.html')} <- {link}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
