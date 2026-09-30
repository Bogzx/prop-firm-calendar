from datetime import UTC, datetime
from pathlib import Path

from prop_firm_calendar.config import AppConfig, CalendarConfig, EventRules, LLMConfig, SourceConfig
from prop_firm_calendar.models import EventType, TradingEvent
from prop_firm_calendar.sinks.null import StateOnlySink

EVENT = TradingEvent(
    event_type=EventType.MAINTENANCE,
    summary="Maintenance",
    description="d",
    start=datetime(2026, 6, 6, 8, 0, tzinfo=UTC),
    end=datetime(2026, 6, 6, 14, 0, tzinfo=UTC),
    source_post_key="p",
    source_url="u",
)


def test_state_only_sink_contract() -> None:
    sink = StateOnlySink()
    assert sink.find_event_id_by_key("anything") is None
    event_id = sink.create_event(EVENT)
    assert event_id == f"ics:{EVENT.event_key}"
    sink.delete_event(event_id)  # must not raise


def test_build_sink_dry_run_needs_no_credentials(tmp_path: Path) -> None:
    """A first-time user must be able to preview with zero Google setup."""
    import prop_firm_calendar.cli as cli

    config = AppConfig(
        source=SourceConfig(),
        llm=LLMConfig(api_key="k"),
        calendar=CalendarConfig(),  # oauth mode, but no token/credentials exist in tmp_path
        events=EventRules(),
        base_dir=tmp_path,
    )
    sink = cli._build_sink(config, dry_run=True)
    assert type(sink).__name__ == "StateOnlySink"


def test_build_sink_calendar_disabled(tmp_path: Path) -> None:
    import prop_firm_calendar.cli as cli

    config = AppConfig(
        source=SourceConfig(),
        llm=LLMConfig(api_key="k"),
        calendar=CalendarConfig(enabled=False),
        events=EventRules(),
        base_dir=tmp_path,
    )
    sink = cli._build_sink(config, dry_run=False)
    assert type(sink).__name__ == "StateOnlySink"
