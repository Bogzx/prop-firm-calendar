"""Telegram bot notifier."""

from __future__ import annotations

import requests


class TelegramNotifier:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    def send(self, text: str) -> None:
        response = requests.post(
            self._url, data={"chat_id": self._chat_id, "text": text}, timeout=10
        )
        response.raise_for_status()
