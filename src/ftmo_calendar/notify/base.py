"""Notification protocol and message formatting.

Notifications are best-effort: a failing channel is logged and never breaks a
run. The sync itself stays the source of truth; this is the visibility layer.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from ftmo_calendar.pipeline import RunReport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventPayload:
    """Structured form of what a run changed, for receivers that want data."""

    created: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @classmethod
    def from_report(cls, report: RunReport) -> EventPayload:
        return cls(
            created=list(report.created_lines),
            removed=list(report.deleted_lines),
            anomalies=list(report.anomalies),
        )


class Notifier(Protocol):
    name: str

    def send(self, text: str) -> None: ...


def notify_all(
    notifiers: Iterable[Notifier], text: str, payload: EventPayload | None = None
) -> None:
    """Send to every channel; a channel that fails is logged, never fatal.

    Channels that implement `send_events` receive the structured payload too —
    a plain `Notifier` only ever needs `send`.
    """
    for notifier in notifiers:
        try:
            rich = getattr(notifier, "send_events", None)
            if payload is not None and callable(rich):
                rich(text, payload)
            else:
                notifier.send(text)
        except Exception as e:  # noqa: BLE001 - channel failure must not break the run
            logger.warning("Notification via %s failed: %s", notifier.name, e)


def format_run_message(report: RunReport) -> str | None:
    """Message describing calendar changes; None when nothing changed."""
    if not report.created_lines and not report.deleted_lines:
        return None
    lines = ["📅 FTMO Calendar updated"]
    lines.extend(f"➕ {line}" for line in report.created_lines)
    lines.extend(f"➖ {line}" for line in report.deleted_lines)
    return "\n".join(lines)


def format_anomaly_message(report: RunReport) -> str | None:
    """Message for a run that completed but produced a suspicious result.

    This is the one that turns "fails loudly" from a claim into a fact: these
    runs exit 0 and look healthy from the outside.
    """
    if not report.anomalies:
        return None
    lines = ["⚠️ ftmo-calendar ran but the result looks wrong:"]
    lines.extend(f"• {anomaly}" for anomaly in report.anomalies)
    lines.append("Check the source page — the site or its wording may have changed.")
    return "\n".join(lines)


def format_error_message(error: BaseException) -> str:
    return (
        f"❌ ftmo-calendar run failed: {error}\n"
        "Check the logs; if it looks auth-related, run `ftmo-calendar auth --check`."
    )


def format_heartbeat_message(report: RunReport) -> str:
    return f"✅ ftmo-calendar alive — last check OK ({report.summary()})"
