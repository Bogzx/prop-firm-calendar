"""Google Calendar credentials.

Two modes:
- service_account: key file + calendar shared with the service account. Never
  expires, no browser — the right choice for servers and cron.
- oauth: token.json produced by the explicit `prop-firm-calendar auth` command.
  `run` NEVER starts an interactive flow (it would hang a headless cron run);
  it refreshes silently or fails with instructions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from prop_firm_calendar.config import CalendarConfig

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_TESTING_MODE_TIP = (
    "Tip: OAuth apps left in 'Testing' publishing status get refresh tokens that expire "
    "every 7 days. In the Google Cloud console, publish your OAuth consent screen to "
    "'Production' to get long-lived tokens."
)


class AuthError(Exception):
    """Credentials missing or invalid; the message tells the user what to do."""


def _resolve(base_dir: Path, filename: str) -> Path:
    p = Path(filename)
    return p if p.is_absolute() else base_dir / p


def load_credentials(cfg: CalendarConfig, base_dir: Path):
    if cfg.auth_mode == "service_account":
        return _load_service_account(cfg, base_dir)
    return _load_oauth(cfg, base_dir)


def _load_service_account(cfg: CalendarConfig, base_dir: Path):
    from google.oauth2 import service_account

    key_path = _resolve(base_dir, cfg.service_account_file)
    if not key_path.exists():
        raise AuthError(
            f"Service account key not found: {key_path}\n"
            "Create a service account in the Google Cloud console, download its JSON key "
            "to that path, and share your calendar with the service account's email "
            "(permission: 'Make changes to events')."
        )
    try:
        return service_account.Credentials.from_service_account_file(str(key_path), scopes=SCOPES)
    except (ValueError, GoogleAuthError) as e:
        raise AuthError(f"Invalid service account key {key_path}: {e}") from e


def _load_oauth(cfg: CalendarConfig, base_dir: Path):
    token_path = _resolve(base_dir, cfg.token_file)
    if not token_path.exists():
        raise AuthError(
            f"No OAuth token at {token_path}. Run `prop-firm-calendar auth` once on a machine "
            "with a browser to authorize."
        )
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except ValueError as e:
        raise AuthError(
            f"OAuth token {token_path} is corrupt: {e}. Run `prop-firm-calendar auth`."
        ) from e
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthError(
                "OAuth token refresh failed (expired or revoked). "
                f"Run `prop-firm-calendar auth` to re-authorize.\n{_TESTING_MODE_TIP}"
            ) from e
        token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info("Refreshed OAuth token")
        return creds
    raise AuthError(
        f"OAuth token at {token_path} is not refreshable. Run `prop-firm-calendar auth`.\n"
        f"{_TESTING_MODE_TIP}"
    )


def interactive_auth(cfg: CalendarConfig, base_dir: Path) -> Path:
    """Run the browser OAuth flow. Only called by the `auth` CLI command."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = _resolve(base_dir, cfg.credentials_file)
    if not creds_path.exists():
        raise AuthError(
            f"OAuth client file not found: {creds_path}\n"
            "Download credentials.json for a 'Desktop app' OAuth client from the "
            "Google Cloud console (APIs & Services → Credentials)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token_path = _resolve(base_dir, cfg.token_file)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return token_path


def describe_credentials(cfg: CalendarConfig, base_dir: Path) -> str:
    """Human-readable status for `prop-firm-calendar auth --check`."""
    if cfg.auth_mode == "service_account":
        key_path = _resolve(base_dir, cfg.service_account_file)
        if not key_path.exists():
            return f"service_account: key file MISSING at {key_path}"
        return f"service_account: key file present at {key_path} (no expiry)"
    token_path = _resolve(base_dir, cfg.token_file)
    if not token_path.exists():
        return f"oauth: NO token at {token_path} — run `prop-firm-calendar auth`"
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except ValueError:
        return f"oauth: token at {token_path} is CORRUPT — run `prop-firm-calendar auth`"
    expiry = creds.expiry.replace(tzinfo=UTC) if creds.expiry else None
    status = "valid" if creds.valid else "expired (will auto-refresh on next run)"
    refresh = "yes" if creds.refresh_token else "NO — re-run `prop-firm-calendar auth`"
    now = datetime.now(UTC)
    expiry_text = (
        f"{expiry.isoformat()} ({'past' if expiry and expiry < now else 'future'})"
        if expiry
        else "unknown"
    )
    return (
        f"oauth: token {status}\n"
        f"  access token expiry: {expiry_text}\n"
        f"  refresh token present: {refresh}\n"
        f"  {_TESTING_MODE_TIP}"
    )
