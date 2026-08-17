# Contributing

Thanks for considering a contribution!

## Development setup

```bash
git clone https://github.com/Bogzx/ftmo-calendar && cd ftmo-calendar
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .[dev]
```

## Before opening a PR

All three must pass — CI runs the same checks:

```bash
ruff check . && ruff format --check .
mypy src
pytest
```

- New behavior needs a test. The suite runs offline: scraping is tested against
  recorded HTML fixtures (`tests/fixtures/<profile>/`), LLM parsing against
  scripted backends, and the HTTP server against a real server on an ephemeral
  port.
- Keep the pipeline seams: new calendar targets implement `EventSink`
  (`sinks/`), new notification channels implement `Notifier` (`notify/`), and
  new announcement sources are **configuration**, not code (below).
- Architecture and design history live in `docs/superpowers/specs/` and
  `docs/superpowers/plans/`.

## Fixtures are recorded, not written

`tests/fixtures/` holds pages captured from the live sites by
`scripts/record_fixtures.py`, with `<script>`, `<style>`, `<svg>`, `<noscript>`
and comments stripped and the `<main>` element kept verbatim. Each file starts
with a provenance comment naming its URL and capture date.

Refresh them after a site redesign:

```bash
python scripts/record_fixtures.py --profile ftmo --posts 2
```

Please don't hand-author fixture markup to make a test pass. A fixture written
to match the parser agrees with it by construction — including when the parser
is wrong, which is exactly when you need the test to disagree.

## Adding a new prop-firm source

A source is a TOML profile plus a fixture; no Python module is needed.

1. Open an issue with the firm's announcements URL — source support is
   demand-driven and we'd like to record real demand before merging.
2. Copy `src/ftmo_calendar/sources/profiles/example-firm.toml`, which documents
   every field, and fill in the page's selectors.
3. Record fixtures: `python scripts/record_fixtures.py --profile <name>`.
4. Add parse tests against them (see `tests/test_source_profile.py` for a firm
   defined entirely in configuration).

Prefer selectors anchored on a class that names the content (`div.post-body`)
over bare tags (`article`). A bare tag also matches teaser cards and navigation,
and a wrong container is worse than no container: the LLM extracts from it
regardless and produces plausible, wrong calendar entries instead of an error.
`min_content_chars` is the backstop — keep it meaningful.

## Golden tests

`tests/test_golden_extraction.py` pins one real announcement to the exact events
it must produce. If you change the prompt, the event taxonomy or validation,
expect it to fail — and update the pinned JSON deliberately, in the same commit,
so the behaviour change is visible in the diff rather than discovered by a
subscriber.
