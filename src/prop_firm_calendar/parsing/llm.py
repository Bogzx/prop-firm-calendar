"""Provider-agnostic LLM extraction with validation, repair retry, and model fallback."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)


class RawEvent(BaseModel):
    """One event as extracted by the model, before validation/normalization."""

    event_type: Literal[
        "maintenance",
        "crypto_closure",
        "holiday_closure",
        "early_close",
        "late_open",
        "symbol_event",
        "other",
    ]
    start_time: str
    end_time: str
    stated_utc_offset: str | None = None
    affected: str | None = None  # symbols/platforms, e.g. "UK100.cash, HK50.cash" or "cTrader"
    confidence: Literal["high", "low"] = "high"


_EVENTS = TypeAdapter(list[RawEvent])


class BackendError(Exception):
    """The LLM API call itself failed (quota, network, refusal)."""


class ExtractionError(Exception):
    """No model produced a valid extraction."""


def _merge_variant(kept: RawEvent, candidate: RawEvent) -> RawEvent:
    """Combine duplicate extractions of one event, keeping the most explicit fields."""
    updates: dict = {}
    if kept.stated_utc_offset is None and candidate.stated_utc_offset:
        updates["stated_utc_offset"] = candidate.stated_utc_offset
    if len(candidate.affected or "") > len(kept.affected or ""):
        updates["affected"] = candidate.affected
    return kept.model_copy(update=updates) if updates else kept


class LLMBackend(Protocol):
    def complete(self, prompt: str, model: str) -> str: ...


PROMPT_TEMPLATE = """You extract scheduled trading interruptions from a prop-firm announcement.

Output ONLY a JSON array, no prose and no markdown fences. Each element:
{{"event_type": "...", "start_time": "YYYY-MM-DDTHH:MM:SS", "end_time": "YYYY-MM-DDTHH:MM:SS", \
"stated_utc_offset": "+03:00" or null, "affected": "..." or null, "confidence": "high"|"low"}}

Event types — classify every scheduled interruption as exactly one of:
- "maintenance": trading platform downtime (MT4, MT5, cTrader, DXtrade). One event per \
distinct window; set "affected" to the platforms (e.g. "all platforms", "cTrader").
- "crypto_closure": crypto symbols closed or unavailable.
- "holiday_closure": symbol(s) closed for the WHOLE day (holiday). Times 00:00:00-23:59:00 \
of that day.
- "early_close": symbol(s) stop trading early. start_time = the early close time, \
end_time = 23:59:00 the same day.
- "late_open": symbol(s) start trading late. start_time = 00:00:00 that day, \
end_time = the late opening time.
- "symbol_event": scheduled forced actions on positions (corporate actions, spin-offs, \
delistings — e.g. "open FDX positions will be closed automatically"). If only a day is \
given, use 00:00:00-23:59:00 and confidence "low".
- "other": any other scheduled trading interruption.

Rules:
- "affected": the symbols or platforms concerned, comma-separated, verbatim from the text \
(e.g. "UK100.cash, HK50.cash, Equities I CFD"). Group symbols sharing the same type and \
times into ONE event. null if everything is affected.
- If the text states a timezone (e.g. "GMT+3"), set stated_utc_offset to it ("+03:00"); else null.
- Ignore anything without a concrete scheduled date (general reminders, swap notices, \
geopolitical advisories).
- Ignore Client Area / website / IT / billing / account-services maintenance — it is not a \
trading interruption.
- Ignore condition changes that interrupt nothing: leverage adjustments, execution-model \
news, permanent session-time changes ("effective from..."), spread or swap updates.
- If there are no scheduled events, output [].
- Set "confidence" to "low" when you had to infer a date or time that the text \
does not state outright; "high" only when the announcement says it plainly.
{hints}
Announcement text:
---
{text}
---
"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class EventExtractor:
    def __init__(
        self,
        backend: LLMBackend,
        models: Sequence[str],
        consensus_runs: int = 1,
        prompt_hints: str = "",
    ) -> None:
        if not models:
            raise ValueError("at least one model is required")
        self.backend = backend
        self.models = list(models)
        self.consensus_runs = max(1, consensus_runs)
        # Firm-specific vocabulary and boilerplate-to-ignore, supplied by the
        # source profile so a new firm needs no prompt edit in Python.
        self.prompt_hints = prompt_hints.strip()

    def extract(self, text: str) -> list[RawEvent]:
        """Extract events; with consensus_runs > 1, majority-vote across runs.

        Hosted APIs (notably OpenRouter, which routes one model id across
        several providers) are not perfectly deterministic even at
        temperature 0. Majority voting across runs makes the reported event
        set stable run-to-run.
        """
        hints = ""
        if self.prompt_hints:
            hints = f"\nAbout this source specifically:\n{self.prompt_hints}\n"
        prompt = PROMPT_TEMPLATE.format(text=text, hints=hints)
        if self.consensus_runs == 1:
            return self._extract_with_fallback(prompt)
        runs = [self._extract_with_fallback(prompt) for _ in range(self.consensus_runs)]
        return self._consensus(runs)

    def _extract_with_fallback(self, prompt: str) -> list[RawEvent]:
        last_error: Exception | None = None
        for model in self.models:
            try:
                return self._extract_once(prompt, model)
            except (BackendError, ExtractionError) as e:
                logger.warning("Model %s failed: %s", model, e)
                last_error = e
        raise ExtractionError(f"all models failed; last error: {last_error}")

    def _consensus(self, runs: list[list[RawEvent]]) -> list[RawEvent]:
        # Identity excludes stated_utc_offset: one announcement has one timezone
        # context, and offset-None resolves to the same instant downstream — an
        # offset-attribution flicker must not split the vote. The most explicit
        # variant wins the merge.
        majority = self.consensus_runs // 2 + 1
        counts: dict[tuple, int] = {}
        merged: dict[tuple, RawEvent] = {}
        order: list[tuple] = []
        for run in runs:
            seen_this_run: set[tuple] = set()
            for event in run:
                key = (event.event_type, event.start_time, event.end_time)
                if key in seen_this_run:
                    continue
                seen_this_run.add(key)
                counts[key] = counts.get(key, 0) + 1
                if key not in merged:
                    merged[key] = event
                    order.append(key)
                else:
                    merged[key] = _merge_variant(merged[key], event)
        kept = [merged[key] for key in order if counts[key] >= majority]
        dropped = [key for key in order if counts[key] < majority]
        if dropped:
            logger.info(
                "Consensus (%d runs) dropped %d minority event(s): %s",
                self.consensus_runs,
                len(dropped),
                dropped,
            )
        return kept

    def _extract_once(self, prompt: str, model: str) -> list[RawEvent]:
        raw = self.backend.complete(prompt, model)
        try:
            return self._parse(raw)
        except ValidationError as first:
            logger.info("Invalid extraction from %s; attempting repair retry", model)
            repair_prompt = (
                f"{prompt}\n\nYour previous reply was invalid: {str(first)[:500]}\n"
                "Reply again with ONLY the corrected JSON array."
            )
            raw = self.backend.complete(repair_prompt, model)
            try:
                return self._parse(raw)
            except ValidationError as second:
                raise ExtractionError(f"invalid JSON after repair retry: {second}") from second

    @staticmethod
    def _parse(raw: str) -> list[RawEvent]:
        cleaned = _THINK.sub("", raw)  # reasoning models (DeepSeek R1, …) inline <think> blocks
        cleaned = _FENCE.sub("", cleaned.strip()).strip()
        try:
            return _EVENTS.validate_json(cleaned)
        except ValidationError:
            # Some models wrap the array in prose despite instructions —
            # fall back to the outermost JSON array in the reply.
            start, end = cleaned.find("["), cleaned.rfind("]")
            if 0 <= start < end:
                return _EVENTS.validate_json(cleaned[start : end + 1])
            raise
