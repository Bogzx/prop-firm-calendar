# Firm candidates that do not ship (yet)

A firm ships when a public page states its scheduled interruptions in text,
robots.txt allows reading it, and a recorded fixture pins what extraction must
produce (CONTRIBUTING.md, "Adding a new prop-firm source"). These were checked
and do not meet the first condition. Recorded so the next attempt starts here
instead of from a search engine.

Checked 2026-09-30 with the project User-Agent (`TradingCalendarBot`), through
the project's own robots-honouring fetcher; seven requests in total.

| Firm | Pages checked | robots.txt | Finding |
| --- | --- | --- | --- |
| **FundedNext** | `help.fundednext.com/en/` (home), collection *Ongoing Offers and Updates* (`/en/collections/5535891`), article *Trading Session Time* (`/en/articles/9857265`) | allows `/en/` (Crawl-delay 1) | Intercom help centre (same stack as Topstep/E8/Blueberry, so the profile would be trivial). But no article carries dated holiday or maintenance schedules: the updates collection is promotions and payouts, *Trading Session Time* is generic session education. Holiday schedules (e.g. Christmas 2025, Easter 2026) are posted on X (`x.com/FundedNext`), as image cards. |
| **The5ers** | `help.the5ers.com/market-trading-hours/`, `help.the5ers.com/` | no robots.txt (404) | Both 404 to a bot today (search engines still index the first). Standard hours are "Mon–Fri 00:05–23:55 EET"; no holiday schedule found on the site. `the5ers.com/tag/trading-hours/` (a blog tag archive) was not fetched and is the next thing to try. |
| **FundingPips** | `help.fundingpips.com/hc/en-us` (home) | standard Zendesk rules; articles allowed | Zendesk help centre; no announcements or schedule section. Holiday schedules are posted on X (`x.com/fundingpips`). |

## What would unblock them

- **A text page.** If any of them starts publishing a holiday/maintenance
  article on its help centre, a profile is a copy of `topstep.toml` (Intercom)
  or a small Zendesk variant plus a recorded fixture and a golden, then an
  `prop-firm-calendar eval` run before it is listed in `[[firms]]`.
- **Not X.** Scraping X is against its terms without the paid API, and the
  schedules are images; an LLM reading OCR output is exactly the "confident,
  wrong calendar entry" this project is built to refuse.
- **Email/Discord announcements** would need an inbox or bot integration — a
  product decision, not a profile.
