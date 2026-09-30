# Security

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability**
on this repository (a private security advisory). Do not open a public issue
for anything that could hurt the public instance or its subscribers. Expect an
answer within a few days; this is a single-maintainer project.

In scope: the code in this repository and the public instance at
calendar.bogdantruta.com — e.g. injection into the ICS feed or status page,
anything that lets a request alter state, SSRF through the scraper, or a way to
make the feed publish a wrong time. Out of scope: rate limiting of the public
instance, and the third-party sites being scraped.

## Secrets

The application reads every secret from the environment (`.env`), never from
`config.toml` or the repository: `LLM_API_KEY`/`GEMINI_API_KEY`, the Discord,
Telegram and webhook URLs, and the Google credential *files* (`token.json`,
`service_account.json`, `credentials.json`), which are git-ignored.

Two layers keep new secrets out of history:

- **CI** (`secrets` job in `.github/workflows/ci.yml`) runs
  [gitleaks](https://github.com/gitleaks/gitleaks) over the commits a push or
  pull request adds, with a pinned, checksum-verified binary. A finding fails
  the build.
- **pre-commit** (optional, local): `pipx install pre-commit && pre-commit
  install` runs the same gitleaks rules on staged changes, plus ruff, before a
  commit exists.

If a scan flags something that is genuinely not a secret, prefer an inline
`# gitleaks:allow` comment on that line over widening `.gitleaks.toml`.

### Known historical exposure

Commit `5224001` (2025-08-16) hardcoded a Google (Gemini) API key in
`check_models.py`. The file was removed in `95063de`, but the commit remains in
the public history and in forks, so **that key must be treated as public and
revoked** in Google AI Studio / Cloud Console. Rewriting history would not
un-publish it. The commit is allowlisted in `.gitleaks.toml` solely so a
full-history scan does not fail on history that cannot be changed; the
allowlist does not make the key safe.

## If a secret leaks

1. Revoke or rotate it at the provider first — that is the only step that
   actually helps.
2. Remove it from the working tree and move it to `.env`.
3. Only then consider whether a history rewrite is worth it (it will not reach
   forks or clones that already have it).
