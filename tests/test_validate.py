from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from prop_firm_calendar.config import EventRules
from prop_firm_calendar.models import EventType, SourcePost
from prop_firm_calendar.parsing.llm import RawEvent
from prop_firm_calendar.parsing.validate import validate_events

TZ = ZoneInfo("Europe/Bucharest")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
POST = SourcePost(
    post_key="trading-update-2026-06-04",
    title="Trading Update | Jun 4 2026",
    text="Maintenance on Saturday. " * 100,
    url="https://ftmo.com/en/trading-updates/",
)


def raw(start="2026-06-06T08:00:00", end="2026-06-06T14:00:00", **kw) -> RawEvent:
    defaults = dict(
        event_type="maintenance",
        start_time=start,
        end_time=end,
        stated_utc_offset="+03:00",
        confidence="high",
    )
    defaults.update(kw)
    return RawEvent(**defaults)


def run(events, rules=None):
    return validate_events(events, POST, rules or EventRules(), TZ, TZ, now=NOW)


def test_valid_event_converted() -> None:
    events, rejections = run([raw()])
    assert rejections == []
    [event] = events
    assert event.event_type is EventType.MAINTENANCE
    assert event.start.isoformat() == "2026-06-06T08:00:00+03:00"
    assert event.summary == EventRules().summaries["maintenance"]
    assert "https://ftmo.com/en/trading-updates/" in event.description
    assert len(event.description) < 1000  # excerpt is trimmed
    assert event.source_post_key == POST.post_key


def test_missing_offset_uses_source_timezone() -> None:
    events, _ = run([raw(stated_utc_offset=None)])
    assert events[0].start.utcoffset() == datetime(2026, 6, 6, tzinfo=TZ).utcoffset()


def test_end_before_start_rejected() -> None:
    events, rejections = run([raw(start="2026-06-06T14:00:00", end="2026-06-06T08:00:00")])
    assert events == [] and "after start" in rejections[0].reason


def test_overlong_duration_rejected() -> None:
    events, rejections = run([raw(end="2026-06-09T08:00:00")])
    assert events == [] and "duration" in rejections[0].reason


def test_too_far_ahead_rejected() -> None:
    events, rejections = run([raw(start="2027-06-06T08:00:00", end="2027-06-06T14:00:00")])
    assert events == [] and "future" in rejections[0].reason


def test_already_ended_rejected() -> None:
    events, rejections = run([raw(start="2026-05-01T08:00:00", end="2026-05-01T14:00:00")])
    assert events == [] and "ended" in rejections[0].reason


def test_unparseable_datetime_rejected() -> None:
    events, rejections = run([raw(start="whenever")])
    assert events == [] and "datetime" in rejections[0].reason


def test_affected_control_characters_stripped() -> None:
    events, _ = run([raw(affected="US30\r\nX-INJECTED:1\x00")])
    assert "\r" not in events[0].summary and "\n" not in events[0].summary
    assert "US30X-INJECTED:1" in events[0].summary


def test_affected_symbols_in_summary() -> None:
    events, _ = run([raw(event_type="early_close", affected="US30.cash, US100.cash")])
    assert events[0].summary == "⏳ Early Close — US30.cash, US100.cash"


def test_long_affected_list_truncated() -> None:
    events, _ = run([raw(affected=", ".join(f"SYM{i}.cash" for i in range(20)))])
    assert len(events[0].summary) < 110
    assert events[0].summary.endswith("…")


def test_granular_types_validate() -> None:
    for event_type in ("holiday_closure", "early_close", "late_open", "symbol_event"):
        events, rejections = run([raw(event_type=event_type)])
        assert rejections == [] and events[0].event_type.value == event_type


# -- confidence -----------------------------------------------------------
# The model is asked for it, consensus votes on it, and until now nothing read
# it: a guess reached subscribers looking exactly as certain as a stated
# maintenance window.


def test_high_confidence_event_is_unmarked() -> None:
    events, _ = run([raw()])
    assert events[0].confidence == "high"
    assert "unconfirmed" not in events[0].summary
    assert "confidence" not in events[0].description.lower()


def test_low_confidence_event_is_flagged_not_hidden() -> None:
    events, _ = run([raw(confidence="low")])
    [event] = events
    assert event.confidence == "low"
    assert event.summary.endswith("(unconfirmed)")
    assert "LOW" in event.description
    assert "https://ftmo.com/en/trading-updates/" in event.description  # source kept


def test_low_confidence_marker_is_configurable() -> None:
    events, _ = run([raw(confidence="low")], EventRules(low_confidence_marker="[guess]"))
    assert events[0].summary.endswith("[guess]")


def test_low_confidence_can_be_rejected_outright() -> None:
    events, rejections = run([raw(confidence="low")], EventRules(reject_low_confidence=True))
    assert events == []
    assert "confidence" in rejections[0].reason


def test_rejecting_low_confidence_keeps_high_confidence_events() -> None:
    events, rejections = run(
        [raw(), raw(start="2026-06-07T08:00:00", end="2026-06-07T14:00:00", confidence="low")],
        EventRules(reject_low_confidence=True),
    )
    assert len(events) == 1 and len(rejections) == 1


def test_confidence_does_not_change_event_identity() -> None:
    """A confidence flicker between runs must not orphan a calendar entry."""
    high, _ = run([raw()])
    low, _ = run([raw(confidence="low")])
    assert high[0].event_key == low[0].event_key


def test_only_a_too_far_event_is_retryable() -> None:
    """Beyond max_days_ahead is 'not yet'; every other rejection is final."""
    _, rejections = run(
        [
            raw("2026-12-25T00:00:00", "2026-12-25T23:59:00"),
            raw("2026-05-01T08:00:00", "2026-05-01T09:00:00"),  # already ended
            raw("2026-06-06T14:00:00", "2026-06-06T08:00:00"),  # end before start
        ]
    )
    assert [(r.reason, r.retryable) for r in rejections] == [
        ("too far in the future", True),
        ("already ended", False),
        ("end is not after start", False),
    ]


# -- evidence spans ----------------------------------------------------------

TABLE_POST = SourcePost(
    post_key="topstep-holiday",
    title="Topstep Holiday Trading Hours",
    text=(
        "2026 Holiday Schedule Holiday Date Close Positions By Reopen After "
        "Thanksgiving Thursday, November 26 11:45 CT 17:00 CT "
        "Christmas Day Friday, December 25 Markets closed 17:00 CT "
    )
    * 3,
    url="https://help.topstep.com/en/articles/13350348",
)


def run_on(post: SourcePost, events, rules=None):
    return validate_events(
        events, post, rules or EventRules(), TZ, TZ, now=datetime(2026, 11, 1, tzinfo=UTC)
    )


def ts_raw(**kw) -> RawEvent:
    return raw("2026-11-26T11:45:00", "2026-11-26T23:59:00", event_type="early_close", **kw)


def test_a_quote_found_in_the_text_is_kept_on_the_event() -> None:
    [event], _ = run_on(
        TABLE_POST, [ts_raw(evidence="Thanksgiving | Thursday, November 26 | 11:45 CT")]
    )
    # Pipes, commas and case do not matter; every word in order does.
    assert event.evidence == "Thanksgiving | Thursday, November 26 | 11:45 CT"
    assert event.confidence == "high"
    assert "Announcement: “Thanksgiving" in event.description


def test_a_quote_not_in_the_text_publishes_the_event_as_unconfirmed() -> None:
    [event], rejections = run_on(
        TABLE_POST, [ts_raw(evidence="Thanksgiving: close by 10:00 CT on November 26")]
    )
    assert rejections == []
    assert event.confidence == "low"
    assert event.summary.endswith("(unconfirmed)")
    assert event.evidence == ""  # never store words the firm did not write


def test_a_missing_quote_changes_nothing_by_default() -> None:
    [event], _ = run_on(TABLE_POST, [ts_raw()])
    assert event.confidence == "high" and event.evidence == ""


def test_require_evidence_rejects_missing_and_unfound_quotes() -> None:
    rules = EventRules(require_evidence=True)
    events, rejections = run_on(
        TABLE_POST,
        [
            ts_raw(),
            ts_raw(evidence="an invented sentence about November"),
            ts_raw(evidence="Thanksgiving Thursday, November 26 11:45 CT"),
        ],
        rules,
    )
    assert len(events) == 1
    assert [r.reason for r in rejections] == [
        "no evidence quoted from the announcement",
        "quoted evidence does not appear in the announcement",
    ]
    assert not any(r.benign or r.retryable for r in rejections)


def test_a_too_short_quote_is_not_evidence() -> None:
    from prop_firm_calendar.parsing.validate import evidence_supported

    assert not evidence_supported("11:45 CT", TABLE_POST.text)
    assert evidence_supported("November 26 11:45 CT", TABLE_POST.text)
    assert not evidence_supported("November 26 11:46 CT", TABLE_POST.text)


def test_a_short_verbatim_quote_is_not_flagged_unconfirmed() -> None:
    """Review: "Christmas Day | Markets closed" style rows were published as (unconfirmed).

    The prompt asks for the shortest passage; a genuine three-word table row
    is too short to prove anything, but it is no sign of invention either.
    """
    [event], rejections = run_on(TABLE_POST, [ts_raw(evidence="Christmas Day | Friday")])
    assert rejections == []
    assert event.confidence == "high"
    assert not event.summary.endswith("(unconfirmed)")
    assert event.evidence == ""  # too short to show as proof
    # Under require_evidence it is still not enough.
    _, [rejection] = run_on(
        TABLE_POST, [ts_raw(evidence="Christmas Day | Friday")], EventRules(require_evidence=True)
    )
    assert "too short" in rejection.reason


def test_invisible_in_word_characters_do_not_break_a_quote() -> None:
    """Review: a soft hyphen (&shy;) in the scraped HTML split the word the model quoted."""
    from prop_firm_calendar.parsing.validate import evidence_supported

    text = "Main­tenance of the cTrader⁠ platform on Saturday 08:00‍ GMT+3"
    assert evidence_supported("Maintenance of the cTrader platform on Saturday", text)
    assert evidence_supported("cTrader platform on Saturday 08:00", text)
    # NBSP and dash variants were already fine; keep it that way.
    assert evidence_supported(
        "Monday, 25 May 2026 - Memorial Day", "Monday, 25 May 2026 – Memorial Day"
    )


def test_a_post_without_text_cannot_refute_its_quote() -> None:
    """Deferred events re-validated after their post left the index page."""
    stub = SourcePost(post_key="topstep-holiday", title="", text="", url=TABLE_POST.url)
    [event], _ = run_on(stub, [ts_raw(evidence="Thanksgiving Thursday, November 26 11:45 CT")])
    assert event.evidence and event.confidence == "high"


def test_evidence_is_cleaned_and_capped() -> None:
    long_quote = "Thanksgiving Thursday, November 26 11:45 CT\n" + "x " * 400
    text = TABLE_POST.text + " " + long_quote
    post = SourcePost("p", "t", text, "u")
    [event], _ = run_on(post, [ts_raw(evidence=long_quote)])
    assert "\n" not in event.evidence
    assert len(event.evidence) <= 301
