"""Construct active notifiers from configuration (channels activate via env vars)."""

from __future__ import annotations

from ftmo_calendar.config import NotifyConfig
from ftmo_calendar.notify.base import Notifier


def make_notifiers(cfg: NotifyConfig) -> list[Notifier]:
    notifiers: list[Notifier] = []
    if cfg.discord_webhook_url:
        from ftmo_calendar.notify.discord import DiscordNotifier

        notifiers.append(DiscordNotifier(cfg.discord_webhook_url))
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        from ftmo_calendar.notify.telegram import TelegramNotifier

        notifiers.append(TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id))
    if cfg.webhook_url:
        from ftmo_calendar.notify.webhook import WebhookNotifier

        notifiers.append(WebhookNotifier(cfg.webhook_url))
    return notifiers
