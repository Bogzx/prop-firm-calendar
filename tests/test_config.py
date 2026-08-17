from pathlib import Path

import pytest

from ftmo_calendar.config import ConfigError, load_config


def test_defaults_without_config_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.toml", env={})
    assert cfg.source.url == "https://ftmo.com/en/trading-updates/"
    assert cfg.llm.provider == "gemini"
    assert cfg.calendar.auth_mode == "oauth"
    assert cfg.calendar.reminders_minutes == (60, 10)
    assert cfg.base_dir == tmp_path
    assert cfg.state_path == tmp_path / "state.json"


def test_toml_overrides(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        """
[llm]
provider = "openai-compatible"
base_url = "https://openrouter.ai/api/v1"
models = ["google/gemini-2.5-flash"]

[calendar]
auth_mode = "service_account"
calendar_id = "abc@group.calendar.google.com"
reminders_minutes = [30]

[events.summaries]
maintenance = "Platform down"
""",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "config.toml", env={})
    assert cfg.llm.provider == "openai-compatible"
    assert cfg.llm.models == ("google/gemini-2.5-flash",)
    assert cfg.calendar.calendar_id == "abc@group.calendar.google.com"
    assert cfg.calendar.reminders_minutes == (30,)
    assert cfg.events.summaries["maintenance"] == "Platform down"
    assert cfg.events.summaries["other"]  # defaults still merged


def test_api_key_from_env(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.toml", env={"LLM_API_KEY": "k1"})
    assert cfg.llm.api_key == "k1"
    legacy = load_config(tmp_path / "config.toml", env={"GEMINI_API_KEY": "k2"})
    assert legacy.llm.api_key == "k2"


def test_service_account_requires_calendar_id(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[calendar]\nauth_mode = "service_account"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="calendar_id"):
        load_config(tmp_path / "config.toml", env={})


def test_invalid_provider_rejected(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[llm]\nprovider = "magic"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="provider"):
        load_config(tmp_path / "config.toml", env={})


def test_config_with_utf8_bom_loads(tmp_path: Path) -> None:
    """Notepad and PowerShell write UTF-8 with a BOM; tomllib alone rejects it."""
    (tmp_path / "config.toml").write_bytes(b'\xef\xbb\xbf[llm]\nprovider = "gemini"\n')
    cfg = load_config(tmp_path / "config.toml", env={})
    assert cfg.llm.provider == "gemini"


def test_invalid_timezone_rejected(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[source]\ntimezone = "Mars/Olympus"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="timezone"):
        load_config(tmp_path / "config.toml", env={})


def test_default_timezone_is_fixed_gmt3_not_a_dst_zone(tmp_path: Path) -> None:
    """FTMO platform time is a fixed GMT+3 all year.

    Europe/Bucharest — the previous default — is GMT+2 from late October to
    late March, so every announcement that omitted its offset was parsed an
    hour early for five months of the year.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    cfg = load_config(tmp_path / "config.toml", env={})
    assert cfg.source.timezone == "Etc/GMT-3"
    assert cfg.calendar.timezone == "Etc/GMT-3"

    tz = ZoneInfo(cfg.source.timezone)
    winter = datetime(2026, 1, 15, 12, tzinfo=tz)
    summer = datetime(2026, 7, 15, 12, tzinfo=tz)
    assert winter.utcoffset() == summer.utcoffset()
    assert winter.strftime("%z") == "+0300"


def test_event_rule_toggles_load_from_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        "[events]\n"
        "reject_low_confidence = true\n"
        'low_confidence_marker = "[guess]"\n'
        "delete_on_empty_extraction = true\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "config.toml", env={})
    assert cfg.events.reject_low_confidence is True
    assert cfg.events.low_confidence_marker == "[guess]"
    assert cfg.events.delete_on_empty_extraction is True


def test_safe_defaults_for_the_destructive_toggles(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.toml", env={})
    assert cfg.events.delete_on_empty_extraction is False
    assert cfg.events.reject_low_confidence is False
    assert cfg.notify.on_anomalies is True


def test_webhook_url_comes_from_the_environment(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.toml", env={"WEBHOOK_URL": "https://hooks.example/x"})
    assert cfg.notify.webhook_url == "https://hooks.example/x"


def test_source_profile_defaults_to_ftmo(tmp_path: Path) -> None:
    assert load_config(tmp_path / "config.toml", env={}).source.profile == "ftmo"


def test_source_profile_is_selectable(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[source]\nprofile = "example-firm"\n', encoding="utf-8")
    assert load_config(tmp_path / "config.toml", env={}).source.profile == "example-firm"
