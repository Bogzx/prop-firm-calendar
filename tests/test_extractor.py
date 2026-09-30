import pytest

from prop_firm_calendar.parsing.llm import (
    BackendError,
    EventExtractor,
    ExtractionError,
    RawEvent,
)

VALID_JSON = (
    '[{"event_type": "maintenance", "start_time": "2026-06-06T08:00:00",'
    ' "end_time": "2026-06-06T14:00:00", "stated_utc_offset": "+03:00",'
    ' "confidence": "high"}]'
)


class ScriptedBackend:
    """Returns queued responses; raises queued exceptions."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, model: str) -> str:
        self.calls.append((prompt, model))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_extracts_valid_events() -> None:
    backend = ScriptedBackend([VALID_JSON])
    events = EventExtractor(backend, ["m1"]).extract("some announcement")
    assert events == [
        RawEvent(
            event_type="maintenance",
            start_time="2026-06-06T08:00:00",
            end_time="2026-06-06T14:00:00",
            stated_utc_offset="+03:00",
            confidence="high",
        )
    ]


def test_strips_markdown_fences() -> None:
    backend = ScriptedBackend(["```json\n" + VALID_JSON + "\n```"])
    events = EventExtractor(backend, ["m1"]).extract("text")
    assert len(events) == 1


def test_repair_retry_on_invalid_json() -> None:
    backend = ScriptedBackend(["not json at all", VALID_JSON])
    events = EventExtractor(backend, ["m1"]).extract("text")
    assert len(events) == 1
    assert len(backend.calls) == 2
    assert "invalid" in backend.calls[1][0].lower()  # repair prompt mentions the failure


def test_falls_back_to_next_model() -> None:
    backend = ScriptedBackend([BackendError("quota"), VALID_JSON])
    events = EventExtractor(backend, ["m1", "m2"]).extract("text")
    assert len(events) == 1
    assert backend.calls[1][1] == "m2"


def test_raises_when_all_models_fail() -> None:
    backend = ScriptedBackend([BackendError("a"), BackendError("b")])
    with pytest.raises(ExtractionError):
        EventExtractor(backend, ["m1", "m2"]).extract("text")


def test_empty_array_is_valid() -> None:
    backend = ScriptedBackend(["[]"])
    assert EventExtractor(backend, ["m1"]).extract("text") == []


def test_strips_reasoning_think_blocks() -> None:
    """DeepSeek R1-style models may inline <think>…</think> before the answer."""
    thinking = "<think>\nThe text mentions maintenance on Saturday...\n[not json]\n</think>\n"
    response = thinking + VALID_JSON
    backend = ScriptedBackend([response])
    events = EventExtractor(backend, ["deepseek/deepseek-r1"]).extract("text")
    assert len(events) == 1


def test_extracts_array_wrapped_in_prose() -> None:
    """Chat models sometimes wrap the JSON in pleasantries despite instructions."""
    response = f"Sure! Here is the extracted data:\n{VALID_JSON}\nLet me know if you need more."
    backend = ScriptedBackend([response])
    events = EventExtractor(backend, ["deepseek/deepseek-chat"]).extract("text")
    assert len(events) == 1


def test_think_block_and_prose_combined() -> None:
    response = f"<think>reasoning [1,2,3] here</think>The answer:\n```json\n{VALID_JSON}\n```"
    backend = ScriptedBackend([response])
    events = EventExtractor(backend, ["m1"]).extract("text")
    assert len(events) == 1


def event_json(start: str, end: str, event_type: str = "maintenance") -> str:
    return (
        f'{{"event_type": "{event_type}", "start_time": "{start}", "end_time": "{end}",'
        ' "stated_utc_offset": "+03:00", "confidence": "high"}'
    )


A = event_json("2026-06-06T08:00:00", "2026-06-06T14:00:00")
B = event_json("2026-06-06T14:00:00", "2026-06-06T22:00:00")
C = event_json("2026-06-06T09:00:00", "2026-06-06T13:00:00")  # the flaky minority event


def test_consensus_keeps_majority_drops_minority() -> None:
    """3 runs: A+B / A+B+C / A+B  ->  A and B kept (3/3), C dropped (1/3)."""
    backend = ScriptedBackend([f"[{A},{B}]", f"[{A},{B},{C}]", f"[{A},{B}]"])
    events = EventExtractor(backend, ["m1"], consensus_runs=3).extract("text")
    assert len(backend.calls) == 3
    starts = [e.start_time for e in events]
    assert starts == ["2026-06-06T08:00:00", "2026-06-06T14:00:00"]


def test_consensus_keeps_two_of_three() -> None:
    backend = ScriptedBackend([f"[{A},{C}]", f"[{A},{C}]", f"[{A}]"])
    events = EventExtractor(backend, ["m1"], consensus_runs=3).extract("text")
    assert len(events) == 2  # C appears in 2/3 runs -> kept


def test_consensus_identity_ignores_confidence() -> None:
    low = A.replace('"confidence": "high"', '"confidence": "low"')
    backend = ScriptedBackend([f"[{A}]", f"[{low}]", f"[{A}]"])
    events = EventExtractor(backend, ["m1"], consensus_runs=3).extract("text")
    assert len(events) == 1  # same event despite differing confidence


def test_consensus_merge_keeps_most_complete_affected() -> None:
    short = A.replace('"confidence"', '"affected": "US30", "confidence"')
    full = A.replace('"confidence"', '"affected": "US30, US100, US500", "confidence"')
    backend = ScriptedBackend([f"[{short}]", f"[{full}]", f"[{short}]"])
    events = EventExtractor(backend, ["m1"], consensus_runs=3).extract("text")
    assert len(events) == 1
    assert events[0].affected == "US30, US100, US500"


def test_consensus_merges_offset_variants_and_keeps_explicit() -> None:
    """The same wall-clock event with offset None vs '+03:00' must not split the vote."""
    no_offset = A.replace('"stated_utc_offset": "+03:00"', '"stated_utc_offset": null')
    backend = ScriptedBackend([f"[{no_offset}]", f"[{A}]", f"[{no_offset}]"])
    events = EventExtractor(backend, ["m1"], consensus_runs=3).extract("text")
    assert len(events) == 1
    assert events[0].stated_utc_offset == "+03:00"  # explicit variant wins the merge


def test_consensus_runs_one_is_single_call() -> None:
    backend = ScriptedBackend([f"[{A}]"])
    events = EventExtractor(backend, ["m1"], consensus_runs=1).extract("text")
    assert len(backend.calls) == 1 and len(events) == 1


def test_consensus_all_empty_is_empty() -> None:
    backend = ScriptedBackend(["[]", "[]", "[]"])
    assert EventExtractor(backend, ["m1"], consensus_runs=3).extract("text") == []


def test_the_prompt_asks_for_verbatim_evidence() -> None:
    backend = ScriptedBackend(["[]"])
    EventExtractor(backend, ["m1"]).extract("text")
    prompt = backend.calls[0][0]
    assert '"evidence"' in prompt and "copied EXACTLY" in prompt


def test_evidence_is_parsed_and_survives_consensus() -> None:
    quoted = A.replace('"confidence"', '"evidence": "Saturday 6 June 08:00", "confidence"')
    backend = ScriptedBackend([f"[{A}]", f"[{quoted}]", f"[{A}]"])
    [event] = EventExtractor(backend, ["m1"], consensus_runs=3).extract("text")
    assert event.evidence == "Saturday 6 June 08:00"
