"""Provider-agnostic LLM extraction: validation, repair retry, model fallback, voting.

Two ways to vote. EventExtractor asks one model N times and keeps the majority
(stable output from a nondeterministic API). PanelExtractor asks several
independent models once each and publishes only what a quorum of them agree on.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
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
    #: Verbatim quote from the announcement that states this event's date and
    #: time. Checked against the scraped text before it is trusted (see
    #: validate.evidence_supported); a quote that is not there is a strong sign
    #: the event is not there either.
    evidence: str | None = None


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
    if not kept.evidence and candidate.evidence:
        updates["evidence"] = candidate.evidence
    return kept.model_copy(update=updates) if updates else kept


class LLMBackend(Protocol):
    def complete(self, prompt: str, model: str) -> str: ...


PROMPT_TEMPLATE = """You extract scheduled trading interruptions from a prop-firm announcement.

Output ONLY a JSON array, no prose and no markdown fences. Each element:
{{"event_type": "...", "start_time": "YYYY-MM-DDTHH:MM:SS", "end_time": "YYYY-MM-DDTHH:MM:SS", \
"stated_utc_offset": "+03:00" or null, "affected": "..." or null, "confidence": "high"|"low", \
"evidence": "..."}}

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
- "evidence": the shortest passage of the announcement, copied EXACTLY (same words, \
same order, no paraphrase, at most ~200 characters), that states this event's date \
and time — e.g. the table row or sentence. It is checked word-for-word against the \
text, so do not fix typos or reformat it.
{hints}
Announcement text:
---
{text}
---
"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def build_prompt(text: str, prompt_hints: str = "") -> str:
    hints = f"\nAbout this source specifically:\n{prompt_hints}\n" if prompt_hints else ""
    return PROMPT_TEMPLATE.format(text=text, hints=hints)


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


def _extract_once(backend: LLMBackend, prompt: str, model: str) -> list[RawEvent]:
    """One model's answer, with one repair retry on an invalid reply."""
    raw = backend.complete(prompt, model)
    try:
        return _parse(raw)
    except ValidationError as first:
        logger.info("Invalid extraction from %s; attempting repair retry", model)
        repair_prompt = (
            f"{prompt}\n\nYour previous reply was invalid: {str(first)[:500]}\n"
            "Reply again with ONLY the corrected JSON array."
        )
        raw = backend.complete(repair_prompt, model)
        try:
            return _parse(raw)
        except ValidationError as second:
            raise ExtractionError(f"invalid JSON after repair retry: {second}") from second


VoteKey = tuple[str, str, str]


def exact_key(event: RawEvent) -> VoteKey:
    """Identity for repeated runs of one model: its output strings, verbatim.

    stated_utc_offset is left out: one announcement has one timezone context,
    and offset-None resolves to the same instant downstream, so an offset
    attribution flicker must not split the vote. So are `affected`,
    confidence and evidence, which are wording, not the window. The most
    explicit variant wins the merge.
    """
    return (event.event_type, event.start_time, event.end_time)


def _instant(value: str) -> str:
    """'08:00' and '08:00:00' are one statement; anything unparseable stays as given."""
    try:
        return datetime.fromisoformat(value).isoformat(timespec="minutes")
    except ValueError:
        return value


def panel_key(event: RawEvent) -> VoteKey:
    """Identity across different models: exact_key, compared as instants.

    One model formats its timestamps the same way every run; models from
    different vendors do not, and "2026-10-03T08:00" against
    "2026-10-03T08:00:00" must count as agreement, not as two events with
    one vote each.
    """
    return (event.event_type, _instant(event.start_time), _instant(event.end_time))


@dataclass(frozen=True)
class Tally:
    event: RawEvent
    voters: tuple[str, ...]


def tally(
    ballots: Sequence[tuple[str, Sequence[RawEvent]]],
    key: Callable[[RawEvent], VoteKey] = exact_key,
) -> list[Tally]:
    """Who extracted what, in first-seen order. One vote per voter per event."""
    voters: dict[VoteKey, list[str]] = {}
    merged: dict[VoteKey, RawEvent] = {}
    for voter, events in ballots:
        for event in events:
            identity = key(event)
            names = voters.setdefault(identity, [])
            if voter in names:
                continue
            names.append(voter)
            merged[identity] = (
                _merge_variant(merged[identity], event) if identity in merged else event
            )
    return [Tally(merged[identity], tuple(names)) for identity, names in voters.items()]


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
        set stable run-to-run. It does not make the extraction any more right:
        every run is the same model reading the same text. For that, see
        PanelExtractor.
        """
        prompt = build_prompt(text, self.prompt_hints)
        if self.consensus_runs == 1:
            return self._extract_with_fallback(prompt)
        runs = [self._extract_with_fallback(prompt) for _ in range(self.consensus_runs)]
        return self._consensus(runs)

    def _extract_with_fallback(self, prompt: str) -> list[RawEvent]:
        last_error: Exception | None = None
        for model in self.models:
            try:
                return _extract_once(self.backend, prompt, model)
            except (BackendError, ExtractionError) as e:
                logger.warning("Model %s failed: %s", model, e)
                last_error = e
        raise ExtractionError(f"all models failed; last error: {last_error}")

    def _consensus(self, runs: list[list[RawEvent]]) -> list[RawEvent]:
        majority = self.consensus_runs // 2 + 1
        counted = tally([(f"run {i}", run) for i, run in enumerate(runs, 1)])
        dropped = [exact_key(t.event) for t in counted if len(t.voters) < majority]
        if dropped:
            logger.info(
                "Consensus (%d runs) dropped %d minority event(s): %s",
                self.consensus_runs,
                len(dropped),
                dropped,
            )
        return [t.event for t in counted if len(t.voters) >= majority]


@dataclass(frozen=True)
class Juror:
    """One model on an extraction panel, and the backend that serves it."""

    name: str
    backend: LLMBackend
    model: str


@dataclass(frozen=True)
class Ballot:
    """One juror's answer for one post. `events` is None when it abstained."""

    juror: str
    events: tuple[RawEvent, ...] | None
    error: str = ""


@dataclass(frozen=True)
class Dispute:
    """An event some of the panel extracted, but too few to publish."""

    event: RawEvent
    voters: tuple[str, ...]
    panel: tuple[str, ...]
    quorum: int

    def describe(self) -> str:
        others = [name for name in self.panel if name not in self.voters]
        return (
            f"only {len(self.voters)} of {len(self.panel)} models extracted it "
            f"(quorum {self.quorum}): {', '.join(self.voters)} did; "
            f"{', '.join(others)} did not"
        )


class PanelExtractor:
    """Several independent models extract the same post; a quorum must agree.

    Repeating one model (EventExtractor's consensus_runs) smooths out sampling
    noise but repeats its systematic misreadings: a model that reads "8:00 CT"
    as 08:00 UTC does so on every run. Models from different vendors misread
    different things, so an event is published only when at least `quorum` of
    them extracted the same (type, start, end) — and an event that fewer
    extracted is reported as a Dispute rather than dropped silently, because a
    lone model may also be the only one that read the announcement right.

    A juror whose call fails abstains, which counts against every event: an
    outage at one vendor makes the panel stricter, never more lenient. With
    fewer answers than the quorum no event could pass, so the extraction
    fails outright instead of reporting an empty announcement.
    """

    def __init__(self, jurors: Sequence[Juror], quorum: int = 0, prompt_hints: str = "") -> None:
        if len(jurors) < 2:
            raise ValueError("a panel needs at least two models")
        names = [juror.name for juror in jurors]
        if len(set(names)) != len(names):
            raise ValueError(f"panel model names must be unique: {names}")
        self.jurors = list(jurors)
        self.quorum = quorum or len(jurors) // 2 + 1
        if not 1 <= self.quorum <= len(jurors):
            raise ValueError(f"quorum must be between 1 and {len(jurors)}, got {quorum}")
        self.prompt_hints = prompt_hints.strip()
        #: Every juror's answer for the last post, for the eval harness: each
        #: model can be scored alone without paying for another call.
        self.last_ballots: list[Ballot] = []
        #: Events the last post's panel disagreed on (see Dispute).
        self.last_disputes: list[Dispute] = []

    def extract(self, text: str) -> list[RawEvent]:
        prompt = build_prompt(text, self.prompt_hints)
        self.last_ballots = []
        self.last_disputes = []
        for juror in self.jurors:
            try:
                events = _extract_once(juror.backend, prompt, juror.model)
            except (BackendError, ExtractionError) as e:
                logger.warning("Panel model %s abstained: %s", juror.name, e)
                self.last_ballots.append(Ballot(juror.name, None, str(e)))
            else:
                self.last_ballots.append(Ballot(juror.name, tuple(events)))

        answered = [(b.juror, b.events) for b in self.last_ballots if b.events is not None]
        if len(answered) < self.quorum:
            failures = "; ".join(f"{b.juror}: {b.error}" for b in self.last_ballots if b.error)
            raise ExtractionError(
                f"only {len(answered)} of {len(self.jurors)} panel models answered and "
                f"{self.quorum} must agree — {failures}"
            )
        panel = tuple(juror.name for juror in self.jurors)
        kept: list[RawEvent] = []
        for counted in tally(answered, key=panel_key):
            if len(counted.voters) >= self.quorum:
                kept.append(counted.event)
            else:
                self.last_disputes.append(
                    Dispute(counted.event, counted.voters, panel, self.quorum)
                )
        for dispute in self.last_disputes:
            logger.warning(
                "Panel disagreement on %s %s–%s: %s",
                dispute.event.event_type,
                dispute.event.start_time,
                dispute.event.end_time,
                dispute.describe(),
            )
        return kept
