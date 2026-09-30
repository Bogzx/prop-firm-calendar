"""State-only sink: no external calendar at all.

Used for dry runs (preview without any Google setup) and feed-only mode
(`[calendar] enabled = false`), where the ICS file generated from state IS the
calendar and subscribers pull it over HTTP.
"""

from __future__ import annotations

from prop_firm_calendar.models import TradingEvent

#: Ids this sink hands out. Nothing exists behind them: a state that switched
#: from feed-only to Google mode must not treat one as a real calendar entry.
PLACEHOLDER_PREFIX = "ics:"


def is_placeholder(backend_id: str) -> bool:
    return backend_id.startswith(PLACEHOLDER_PREFIX)


class StateOnlySink:
    def find_event_id_by_key(self, event_key: str) -> str | None:
        return None

    def create_event(self, event: TradingEvent) -> str:
        return f"{PLACEHOLDER_PREFIX}{event.event_key}"

    def delete_event(self, event_id: str) -> None:
        return None
