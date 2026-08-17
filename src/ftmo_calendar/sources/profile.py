"""Declarative source profiles: a prop firm is a TOML file, not a Python module.

A profile says where the announcement lives on a page and how to recognise it.
Everything else — fetching, retrying, date parsing, post identity, the LLM
extraction, validation and reconcile — is already firm-agnostic, so adding
FundedNext or The5ers means writing one of these plus a recorded fixture.

Profiles ship in `sources/profiles/*.toml`; `[source] profile = "name"` selects
one, or the value may be a path to a TOML file outside the package.

Selectors are deliberately *required* and deliberately narrow. An earlier
version fell back to `soup.select_one("article, div.entry-content")`, which on
the real FTMO post page matches an `article.post-card` teaser from the
related-posts strip — the scraper would have handed a list of headlines to the
LLM, which would have dutifully extracted plausible, wrong calendar entries.
Structural drift must raise, not guess: see `min_content_chars` and
`WebSource.parse_post`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ftmo_calendar.config import ConfigError

PROFILE_DIR = Path(__file__).parent / "profiles"


@dataclass(frozen=True)
class SourceProfile:
    """Everything firm-specific about scraping one announcements page."""

    name: str
    #: Human-readable firm name, used in messages and the status page.
    display_name: str
    #: The announcements index page.
    url: str
    #: Ordered CSS selectors for the announcement body on a *detail* page. The
    #: first that matches and passes the length check wins; none matching is an
    #: error, never a guess.
    post_content_selectors: tuple[str, ...]
    #: Ordered CSS selectors for the announcement body embedded in the *index*
    #: page (many firms inline the newest post). Empty = the index only links.
    listing_content_selectors: tuple[str, ...] = ()
    #: Selector for the index page's embedded-post title.
    listing_title_selector: str = "h1"
    #: Substring the index title must contain (lowercased) to count as a post.
    listing_title_contains: str = ""
    #: Selector for the cards/rows linking to older posts on the index page.
    link_selectors: tuple[str, ...] = ("article.post-card",)
    #: A discovered href must contain this to be followed — keeps navigation,
    #: pagination and tracking links out of the fetch list.
    link_url_contains: str = ""
    #: Selector for the title on a detail page.
    post_title_selector: str = "h1"
    #: Shorter than this and the "announcement" is a nav blob or a teaser card,
    #: not a post. Raising here is the whole point of the profile.
    min_content_chars: int = 200
    #: Prefix for generated post keys (state identity; keep stable per firm).
    post_key_prefix: str = "trading-update"
    #: Fixed IANA zone the firm states its times in when an announcement omits
    #: an offset. Overridden by [source] timezone when that is set explicitly.
    timezone: str = "Etc/GMT-3"
    #: Keyword gate for relevance, overridden by [source] keywords when set.
    keywords: tuple[str, ...] = ()
    #: Free text appended to the extraction prompt: house vocabulary, symbol
    #: naming, quirks the model should know about this firm specifically.
    prompt_hints: str = ""
    #: Notes for maintainers; ignored at runtime.
    notes: str = field(default="", repr=False)


def _tuple(raw: object, key: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list) and all(isinstance(v, str) for v in raw):
        return tuple(raw)
    raise ConfigError(f"source profile key {key!r} must be a string or a list of strings")


def load_profile(name: str) -> SourceProfile:
    """Load a bundled profile by name, or any TOML file by path."""
    path = PROFILE_DIR / f"{name}.toml"
    if not path.exists():
        candidate = Path(name)
        if candidate.suffix == ".toml" and candidate.exists():
            path = candidate
        else:
            raise ConfigError(
                f"unknown source profile {name!r}; bundled profiles: "
                f"{', '.join(available_profiles())} "
                "(or give a path to your own .toml)"
            )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"cannot parse source profile {path}: {e}") from e
    return profile_from_dict(data, default_name=path.stem)


def profile_from_dict(data: dict, *, default_name: str) -> SourceProfile:
    for required in ("url", "post_content_selectors"):
        if required not in data:
            raise ConfigError(f"source profile is missing required key {required!r}")
    tuple_keys = (
        "post_content_selectors",
        "listing_content_selectors",
        "link_selectors",
        "keywords",
    )
    kwargs: dict = {k: v for k, v in data.items() if k not in tuple_keys}
    for key in tuple_keys:
        if key in data:
            kwargs[key] = _tuple(data[key], key)
    kwargs.setdefault("name", default_name)
    kwargs.setdefault("display_name", kwargs["name"].upper())
    try:
        return SourceProfile(**kwargs)
    except TypeError as e:
        name = kwargs.get("name", default_name)
        raise ConfigError(f"invalid source profile {name!r}: {e}") from e


def available_profiles() -> list[str]:
    if not PROFILE_DIR.is_dir():  # pragma: no cover - always shipped
        return []
    return sorted(p.stem for p in PROFILE_DIR.glob("*.toml"))
