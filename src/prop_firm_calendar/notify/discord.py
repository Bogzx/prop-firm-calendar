"""Discord webhook notifier — a single POST, no bot setup required."""

from __future__ import annotations

import requests


class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    def send(self, text: str) -> None:
        response = requests.post(self._webhook_url, json={"content": text}, timeout=10)
        response.raise_for_status()
