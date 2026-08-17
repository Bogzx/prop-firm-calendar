"""Configuration loading: config.toml with environment-variable overrides for secrets."""

from __future__ import annotations

import dataclasses
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Invalid or missing configuration."""


DEFAULT_SUMMARIES: dict[str, str] = {
    "maintenance": "⚠️ Platform Maintenance",
    "crypto_closure": "🚫 Crypto Closed",
    "holiday_closure": "🏖️ Closed All Day",
    "early_close": "⏳ Early Close",
    "late_open": "🕗 Late Open",
    "symbol_event": "📌 Forced Action",
    "other": "ℹ️ FTMO Trading Update",
    "holiday_hours": "🕒 Modified Trading Hours",  # legacy state entries only
}


# FTMO states every announcement in "MetaTrader platform time — GMT+3", which is
# a *fixed* offset that does not observe daylight saving. Europe/Bucharest happens
# to equal GMT+3 in summer but is GMT+2 from late October to late March, so using
# it as the default parsed every offset-less announcement an hour early for five
# months of the year. Etc/GMT-3 is the IANA zone for a fixed UTC+03:00 (the sign
# in Etc/ names is inverted by POSIX convention).
FTMO_PLATFORM_TZ = "Etc/GMT-3"


@dataclass(frozen=True)
class SourceConfig:
    url: str = "https://ftmo.com/en/trading-updates/"
    keywords: tuple[str, ...] = ("maintenance", "market is closed", "ctrader", "holiday", "crypto")
    timezone: str = FTMO_PLATFORM_TZ
    max_posts: int = 4
    max_age_days: int = 14
    #: Name of a source profile in sources/profiles (see sources.profile). The
    #: default keeps the built-in FTMO scraper; any other value loads that
    #: profile's declarative selectors, so a new prop firm is a TOML file.
    profile: str = "ftmo"


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "gemini"  # "gemini" | "openai-compatible"
    base_url: str = ""  # e.g. https://openrouter.ai/api/v1
    models: tuple[str, ...] = ("gemini-2.5-flash", "gemini-2.0-flash")
    consensus_runs: int = 3  # majority-vote across N extractions for stable results
    api_key: str = ""  # from LLM_API_KEY / GEMINI_API_KEY env, never from TOML


@dataclass(frozen=True)
class CalendarConfig:
    enabled: bool = True  # false = feed-only mode: no Google account needed at all
    auth_mode: str = "oauth"  # "oauth" | "service_account"
    name: str = "Trading"
    calendar_id: str = ""  # required for service_account; optional override for oauth
    # The feed renders wall-clock times in this zone (0.8.1). To read as FTMO
    # announced them all year — not only during European summer time — that has
    # to be the same fixed GMT+3 the announcements are written in.
    timezone: str = FTMO_PLATFORM_TZ
    reminders_minutes: tuple[int, ...] = (60, 10)
    credentials_file: str = "credentials.json"
    token_file: str = "token.json"
    service_account_file: str = "service_account.json"


@dataclass(frozen=True)
class NotifyConfig:
    on_events: bool = True
    on_errors: bool = True
    on_anomalies: bool = True  # alert when a run succeeds but looks wrong
    heartbeat_hours: int = 0  # 0 = heartbeat disabled
    # Channel secrets come from env vars, never from TOML:
    discord_webhook_url: str = ""  # DISCORD_WEBHOOK_URL
    telegram_bot_token: str = ""  # TELEGRAM_BOT_TOKEN
    telegram_chat_id: str = ""  # TELEGRAM_CHAT_ID
    webhook_url: str = ""  # WEBHOOK_URL — generic JSON POST (Slack, n8n, your own)


@dataclass(frozen=True)
class IcsConfig:
    enabled: bool = False
    path: str = "ftmo-events.ics"


@dataclass(frozen=True)
class ServeConfig:
    host: str = "0.0.0.0"  # noqa: S104 - explicit opt-in via the serve command
    port: int = 8080
    sync_interval_minutes: int = 360


@dataclass(frozen=True)
class EventRules:
    max_duration_hours: int = 48
    max_days_ahead: int = 120
    #: Drop extractions the model marked "low" instead of publishing them. Off
    #: by default: a flagged guess is more useful to a subscriber than silence.
    reject_low_confidence: bool = False
    #: Mark low-confidence events in their title so subscribers can tell a
    #: certain window from an inferred one.
    low_confidence_marker: str = "(unconfirmed)"
    #: Allow a post whose extraction lost events — collapsed to zero, or shrank
    #: to a subset with nothing new extracted — to delete the future events it
    #: had created. Off by default — see pipeline._reconcile.
    delete_on_empty_extraction: bool = False
    summaries: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SUMMARIES))


@dataclass(frozen=True)
class AppConfig:
    source: SourceConfig
    llm: LLMConfig
    calendar: CalendarConfig
    events: EventRules
    base_dir: Path
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    ics: IcsConfig = field(default_factory=IcsConfig)
    serve: ServeConfig = field(default_factory=ServeConfig)

    @property
    def state_path(self) -> Path:
        return self.base_dir / "state.json"

    def resolve(self, filename: str) -> Path:
        """Resolve a configured filename relative to the config directory."""
        p = Path(filename)
        return p if p.is_absolute() else self.base_dir / p


def _section(cls: type, data: dict, name: str):  # noqa: ANN202 - generic dataclass factory
    raw = data.get(name, {})
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in raw:
            value = raw[f.name]
            if isinstance(value, list):
                value = tuple(value)
            kwargs[f.name] = value
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise ConfigError(f"invalid [{name}] section: {e}") from e


def _validate(cfg: AppConfig) -> None:
    if cfg.llm.provider not in ("gemini", "openai-compatible"):
        raise ConfigError(
            f"unknown llm provider {cfg.llm.provider!r}; use 'gemini' or 'openai-compatible'"
        )
    if cfg.calendar.auth_mode not in ("oauth", "service_account"):
        raise ConfigError(
            f"unknown auth_mode {cfg.calendar.auth_mode!r}; use 'oauth' or 'service_account'"
        )
    if (
        cfg.calendar.enabled
        and cfg.calendar.auth_mode == "service_account"
        and not cfg.calendar.calendar_id
    ):
        raise ConfigError(
            "calendar.calendar_id is required with auth_mode='service_account' — create the "
            "calendar in Google Calendar, share it with the service account email, and put its "
            "ID here"
        )
    if not cfg.llm.models:
        raise ConfigError("llm.models must list at least one model")
    if cfg.llm.consensus_runs < 1:
        raise ConfigError("llm.consensus_runs must be at least 1")
    for tz_name in (cfg.source.timezone, cfg.calendar.timezone):
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(f"invalid timezone {tz_name!r}: {e}") from e


def load_config(path: Path, env: Mapping[str, str] | None = None) -> AppConfig:
    """Load config from a TOML file (all keys optional) plus env-var secrets.

    A missing file is fine — every setting has a default. The config file's
    directory becomes the base for relative paths (token, state, …).
    """
    env_map = dict(os.environ if env is None else env)
    data: dict = {}
    if path.exists():
        try:
            # utf-8-sig: tolerate the BOM that Notepad/PowerShell prepend on Windows
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"cannot parse {path}: {e}") from e

    source = _section(SourceConfig, data, "source")
    llm = _section(LLMConfig, data, "llm")
    calendar = _section(CalendarConfig, data, "calendar")

    events_raw = data.get("events", {})
    if not isinstance(events_raw, dict):
        raise ConfigError("[events] must be a TOML table")
    summaries = {**DEFAULT_SUMMARIES, **events_raw.get("summaries", {})}
    events = EventRules(
        max_duration_hours=events_raw.get("max_duration_hours", EventRules.max_duration_hours),
        max_days_ahead=events_raw.get("max_days_ahead", EventRules.max_days_ahead),
        reject_low_confidence=events_raw.get(
            "reject_low_confidence", EventRules.reject_low_confidence
        ),
        low_confidence_marker=events_raw.get(
            "low_confidence_marker", EventRules.low_confidence_marker
        ),
        delete_on_empty_extraction=events_raw.get(
            "delete_on_empty_extraction", EventRules.delete_on_empty_extraction
        ),
        summaries=summaries,
    )

    api_key = env_map.get("LLM_API_KEY", "") or env_map.get("GEMINI_API_KEY", "")
    llm = dataclasses.replace(llm, api_key=api_key)

    notify = _section(NotifyConfig, data, "notify")
    notify = dataclasses.replace(
        notify,
        discord_webhook_url=env_map.get("DISCORD_WEBHOOK_URL", ""),
        telegram_bot_token=env_map.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=env_map.get("TELEGRAM_CHAT_ID", ""),
        webhook_url=env_map.get("WEBHOOK_URL", ""),
    )

    cfg = AppConfig(
        source=source,
        llm=llm,
        calendar=calendar,
        events=events,
        base_dir=path.resolve().parent,
        notify=notify,
        ics=_section(IcsConfig, data, "ics"),
        serve=_section(ServeConfig, data, "serve"),
    )
    _validate(cfg)
    return cfg
