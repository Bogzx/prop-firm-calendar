from pathlib import Path

import pytest

from prop_firm_calendar.config import CalendarConfig
from prop_firm_calendar.sinks.auth import AuthError, load_credentials


def test_oauth_without_token_gives_actionable_error(tmp_path: Path) -> None:
    cfg = CalendarConfig(auth_mode="oauth")
    with pytest.raises(AuthError, match="prop-firm-calendar auth"):
        load_credentials(cfg, tmp_path)


def test_service_account_missing_key_file(tmp_path: Path) -> None:
    cfg = CalendarConfig(auth_mode="service_account", calendar_id="x@group.calendar.google.com")
    with pytest.raises(AuthError, match="service_account.json"):
        load_credentials(cfg, tmp_path)
