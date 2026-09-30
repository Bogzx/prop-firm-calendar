from datetime import UTC, datetime

from prop_firm_calendar.models import EventType, SourcePost, TradingEvent


def make_event(**overrides) -> TradingEvent:
    defaults = dict(
        event_type=EventType.MAINTENANCE,
        summary="Maintenance",
        description="desc",
        start=datetime(2026, 6, 6, 8, 0, tzinfo=UTC),
        end=datetime(2026, 6, 6, 14, 0, tzinfo=UTC),
        source_post_key="trading-update-2026-06-04",
        source_url="https://example.com/post",
    )
    defaults.update(overrides)
    return TradingEvent(**defaults)


def test_event_key_is_deterministic() -> None:
    assert make_event().event_key == make_event().event_key


def test_event_key_changes_with_times() -> None:
    other = make_event(end=datetime(2026, 6, 6, 22, 0, tzinfo=UTC))
    assert make_event().event_key != other.event_key


def test_event_key_changes_with_post() -> None:
    other = make_event(source_post_key="trading-update-2026-06-11")
    assert make_event().event_key != other.event_key


def test_content_hash_is_stable() -> None:
    a = SourcePost(post_key="k", title="t", text="same text", url="u")
    b = SourcePost(post_key="k", title="t", text="same text", url="u")
    assert a.content_hash == b.content_hash
    c = SourcePost(post_key="k", title="t", text="different", url="u")
    assert a.content_hash != c.content_hash
