"""Extraction eval: the real prompt, a real model, every golden fixture, N times.

The golden tests pin hand-verified `expected.json` files and never call a
model, so they prove validation and timezone handling but say nothing about
whether today's model, behind today's provider, still extracts those events.
The live feed has drifted without anything failing (Topstep rows lost their
`affected` text). This harness closes that gap:

    prop-firm-calendar --config eval.toml eval --fixtures tests/fixtures --runs 3

For every `tests/fixtures/<profile>/<name>.expected.json` it rebuilds the post
exactly as the scraper would (from `<name>.html` via the detail-page parser, or
from `listing.html` via the index parser), runs the production extractor —
same prompt, same per-firm hints, same consensus — `runs` times, and compares
each run's raw events with the expected ones on (type, start, end).

A run fails the gate on any missing event, any extra event, or a stated offset
that differs from the expected one (a wrong offset is an hour-off calendar).
Instability across runs, missing `affected` text and unverifiable evidence are
reported but do not gate by default.

It costs real LLM calls — fixtures x runs x consensus_runs of them — which is
why it runs from a manual/scheduled workflow with a secret, never in CI.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from prop_firm_calendar.models import SourcePost
from prop_firm_calendar.parsing.llm import RawEvent
from prop_firm_calendar.parsing.validate import evidence_supported
from prop_firm_calendar.sources.profile import SourceProfile, load_profile
from prop_firm_calendar.sources.web import WebSource

Identity = tuple[str, str, str]


class EvalError(Exception):
    """The fixtures cannot be evaluated (missing page, unknown profile, …)."""


@dataclass(frozen=True)
class Case:
    firm: str
    name: str
    profile: SourceProfile
    post: SourcePost
    expected: tuple[RawEvent, ...]


def _identity(event: RawEvent) -> Identity:
    return (event.event_type, event.start_time, event.end_time)


def _offset(value: str | None) -> str | None:
    """'+3', 'GMT+03:00' and '+03:00' are the same statement; None stays None."""
    if not value:
        return None
    text = value.strip().upper().removeprefix("UTC").removeprefix("GMT")
    sign, rest = (text[0], text[1:]) if text[:1] in "+-" else ("+", text)
    hours, _, minutes = rest.partition(":")
    try:
        return f"{sign}{int(hours):02d}:{int(minutes or 0):02d}"
    except ValueError:
        return value.strip()


def discover(fixtures: Path, firms: Sequence[str] | None = None) -> list[Case]:
    """Every `<profile>/<name>.expected.json` under `fixtures`, as a runnable case."""
    if not fixtures.is_dir():
        raise EvalError(f"fixtures directory {fixtures} does not exist")
    cases: list[Case] = []
    for expected_path in sorted(fixtures.glob("*/*.expected.json")):
        firm = expected_path.parent.name
        if firms and firm not in firms:
            continue
        name = expected_path.name.removesuffix(".expected.json")
        profile = load_profile(firm)
        data = json.loads(expected_path.read_text(encoding="utf-8"))
        source = WebSource(profile)
        page = expected_path.with_name(f"{name}.html")
        listing = expected_path.with_name("listing.html")
        if page.exists():
            post = source.parse_post(
                page.read_text(encoding="utf-8"), str(data.get("_source") or profile.url)
            )
        elif listing.exists():
            embedded, _ = source.parse_listing(listing.read_text(encoding="utf-8"))
            if embedded is None:
                raise EvalError(f"{listing} has no embedded post for {expected_path.name}")
            post = embedded
        else:
            raise EvalError(f"no {page.name} or listing.html next to {expected_path}")
        expected = tuple(RawEvent(**e) for e in data["events"])
        cases.append(Case(firm, name, profile, post, expected))
    if not cases:
        raise EvalError(f"no *.expected.json fixtures under {fixtures}")
    return cases


@dataclass
class RunScore:
    matched: int
    missing: list[Identity]
    extra: list[Identity]
    offset_mismatches: list[str]
    affected_missing: list[Identity]
    evidence_verified: int
    evidence_total: int
    error: str | None = None

    def passed(self, max_missing: int, max_extra: int) -> bool:
        return (
            self.error is None
            and len(self.missing) <= max_missing
            and len(self.extra) <= max_extra
            and not self.offset_mismatches
        )


def score(case: Case, got: Sequence[RawEvent]) -> RunScore:
    expected = {_identity(e): e for e in case.expected}
    extracted = {_identity(e): e for e in got}
    matched = expected.keys() & extracted.keys()
    offsets = [
        f"{key[0]} {key[1]}: expected {expected[key].stated_utc_offset!r}, "
        f"got {extracted[key].stated_utc_offset!r}"
        for key in sorted(matched)
        if _offset(expected[key].stated_utc_offset) != _offset(extracted[key].stated_utc_offset)
    ]
    affected = [
        key for key in sorted(matched) if expected[key].affected and not extracted[key].affected
    ]
    quoted = [e for e in got if e.evidence]
    return RunScore(
        matched=len(matched),
        missing=sorted(expected.keys() - extracted.keys()),
        extra=sorted(extracted.keys() - expected.keys()),
        offset_mismatches=offsets,
        affected_missing=affected,
        evidence_verified=sum(
            1 for e in quoted if e.evidence and evidence_supported(e.evidence, case.post.text)
        ),
        evidence_total=len(got),
    )


@dataclass
class CaseResult:
    case: Case
    runs: list[RunScore] = field(default_factory=list)
    identity_sets: list[frozenset[Identity]] = field(default_factory=list)

    @property
    def stable(self) -> bool:
        return len(set(self.identity_sets)) <= 1

    def passed(self, max_missing: int, max_extra: int) -> bool:
        return bool(self.runs) and all(r.passed(max_missing, max_extra) for r in self.runs)

    def as_dict(self, max_missing: int, max_extra: int) -> dict:
        return {
            "firm": self.case.firm,
            "fixture": self.case.name,
            "expected": len(self.case.expected),
            "passed": self.passed(max_missing, max_extra),
            "stable": self.stable,
            "runs": [
                {
                    "matched": r.matched,
                    "missing": [list(k) for k in r.missing],
                    "extra": [list(k) for k in r.extra],
                    "offset_mismatches": r.offset_mismatches,
                    "affected_missing": [list(k) for k in r.affected_missing],
                    "evidence_verified": r.evidence_verified,
                    "evidence_total": r.evidence_total,
                    "error": r.error,
                }
                for r in self.runs
            ],
        }


@dataclass
class Report:
    results: list[CaseResult]
    runs: int
    max_missing: int = 0
    max_extra: int = 0

    @property
    def passed(self) -> bool:
        return all(r.passed(self.max_missing, self.max_extra) for r in self.results)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "runs": self.runs,
            "gate": {"max_missing": self.max_missing, "max_extra": self.max_extra},
            "cases": [r.as_dict(self.max_missing, self.max_extra) for r in self.results],
        }

    def to_markdown(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"## Extraction eval: {verdict}",
            "",
            f"{self.runs} run(s) per fixture; gate: missing ≤ {self.max_missing}, "
            f"extra ≤ {self.max_extra}, no offset mismatches.",
            "",
            "| Firm | Fixture | Expected | Matched per run | Missing | Extra | Offsets | "
            "`affected` lost | Evidence verified | Stable | Result |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for result in self.results:
            runs = result.runs
            verified = sum(r.evidence_verified for r in runs)
            total = sum(r.evidence_total for r in runs)
            lines.append(
                f"| {result.case.firm} | {result.case.name} | {len(result.case.expected)} | "
                f"{', '.join(str(r.matched) if r.error is None else 'error' for r in runs)} | "
                f"{max((len(r.missing) for r in runs), default=0)} | "
                f"{max((len(r.extra) for r in runs), default=0)} | "
                f"{max((len(r.offset_mismatches) for r in runs), default=0)} | "
                f"{max((len(r.affected_missing) for r in runs), default=0)} | "
                f"{verified}/{total} | {'yes' if result.stable else 'NO'} | "
                f"{'pass' if result.passed(self.max_missing, self.max_extra) else '**FAIL**'} |"
            )
        details = []
        for result in self.results:
            for index, run in enumerate(result.runs, 1):
                problems = (
                    [f"missing {' '.join(k)}" for k in run.missing]
                    + [f"extra {' '.join(k)}" for k in run.extra]
                    + [f"offset {m}" for m in run.offset_mismatches]
                    + ([f"error: {run.error}"] if run.error else [])
                )
                if problems:
                    details.append(f"- {result.case.firm}/{result.case.name} run {index}:")
                    details.extend(f"  - {p}" for p in problems)
        if details:
            lines += ["", "### Differences", "", *details]
        return "\n".join(lines) + "\n"


def evaluate(
    cases: Sequence[Case],
    make_extractor: Callable[[SourceProfile], object],
    runs: int = 3,
    *,
    max_missing: int = 0,
    max_extra: int = 0,
) -> Report:
    """Run every case `runs` times with a fresh production extractor per firm."""
    results: list[CaseResult] = []
    for case in cases:
        extractor = make_extractor(case.profile)
        result = CaseResult(case)
        for _ in range(max(1, runs)):
            try:
                got = list(extractor.extract(case.post.text))  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001 - an erroring run is a failed run, not a crash
                result.runs.append(RunScore(0, [], [], [], [], 0, 0, error=str(e)))
                result.identity_sets.append(frozenset())
                continue
            result.runs.append(score(case, got))
            result.identity_sets.append(frozenset(_identity(e) for e in got))
        results.append(result)
    return Report(results, max(1, runs), max_missing, max_extra)
