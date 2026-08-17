"""Generic JSON webhook — one POST to whatever URL you own.

An ICS feed is pull-based and quiet: a subscriber learns about a maintenance
window whenever their calendar app next polls, which can be hours. This pushes
the moment a new interruption is detected, to anything that accepts a JSON
POST — Slack and Mattermost incoming webhooks, n8n/Zapier, or your own service.

The payload carries both a rendered `text` (so Slack-shaped receivers work with
no configuration at all) and structured `events`, so a receiver that wants the
individual windows does not have to parse prose.
"""

from __future__ import annotations

import requests

from ftmo_calendar.notify.base import EventPayload


class WebhookNotifier:
    name = "webhook"

    def __init__(self, url: str, timeout: int = 10) -> None:
        self._url = url
        self._timeout = timeout

    def send(self, text: str) -> None:
        self._post({"text": text, "kind": "message"})

    def send_events(self, text: str, payload: EventPayload) -> None:
        self._post(
            {
                "text": text,
                "kind": "events",
                "created": payload.created,
                "removed": payload.removed,
                "anomalies": payload.anomalies,
            }
        )

    def _post(self, body: dict) -> None:
        response = requests.post(self._url, json=body, timeout=self._timeout)
        response.raise_for_status()
