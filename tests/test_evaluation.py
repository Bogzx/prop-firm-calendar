"""The extraction eval harness, driven by a mocked backend — no live LLM calls.

The backend answers from the golden fixtures themselves, so a perfect model is
simulated exactly and every kind of drift the harness exists to catch can be
injected deliberately.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import prop_firm_calendar.cli as cli
from prop_firm_calendar.evaluation import Case, _offset, discover, evaluate
from prop_firm_calendar.parsing.llm import BackendError, EventExtractor

FIXTURES = Path(__file__).parent / "fixtures"
CASES = discover(FIXTURES)

Transform = Callable[[Case, list[dict]], list[dict]]


def _quote(case: Case) -> str:
    return " ".join(case.post.text.split()[:8])


def perfect(case: Case, events: list[dict]) -> list[dict]:
    return [{**e, "evidence": _quote(case)} for e in events]


class FixtureBackend:
    """Answers each prompt with its fixture's expected events, transformed."""

    def __init__(self, transform: Transform = perfect) -> None:
        self.transform = transform
        self.calls = 0

    def complete(self, prompt: str, model: str) -> str:
        self.calls += 1
        case = next(c for c in CASES if c.post.text in prompt)
        events = [e.model_dump() for e in case.expected]
        return json.dumps(self.transform(case, events))


def run(transform: Transform = perfect, runs: int = 2, consensus: int = 1):
    backend = FixtureBackend(transform)
    report = evaluate(
        CASES,
        lambda profile: EventExtractor(
            backend, ["m"], consensus_runs=consensus, prompt_hints=profile.prompt_hints
        ),
        runs=runs,
    )
    return report, backend


def only(firm: str, change: Transform) -> Transform:
    return lambda case, events: change(case, events) if case.firm == firm else perfect(case, events)


# -- discovery ---------------------------------------------------------------


def test_every_golden_fixture_becomes_a_case() -> None:
    by_firm = {c.firm: c for c in CASES}
    assert set(by_firm) == {"ftmo", "topstep", "blueberry-funded", "e8-markets"}
    assert len(by_firm["topstep"].expected) == 13
    # FTMO's golden is a detail page, the others are embedded in their listing.
    assert by_firm["ftmo"].post.post_key == "trading-update-2026-05-21"
    assert by_firm["topstep"].post.url.startswith("https://help.topstep.com/")


def test_discovery_can_be_narrowed_and_fails_loudly(tmp_path: Path) -> None:
    from prop_firm_calendar.evaluation import EvalError

    assert [c.firm for c in discover(FIXTURES, ["e8-markets"])] == ["e8-markets"]
    with pytest.raises(EvalError, match="does not exist"):
        discover(tmp_path / "nope")
    with pytest.raises(EvalError, match="no \\*.expected.json"):
        discover(tmp_path)


# -- scoring -----------------------------------------------------------------


def test_a_model_that_matches_the_goldens_passes() -> None:
    report, backend = run(runs=3)
    assert report.passed
    assert backend.calls == len(CASES) * 3
    assert all(r.stable for r in report.results)
    for result in report.results:
        assert all(r.evidence_verified == r.evidence_total > 0 for r in result.runs)
    assert "## Extraction eval: PASS" in report.to_markdown()


def test_a_dropped_event_fails_and_is_named() -> None:
    report, _ = run(only("topstep", lambda case, events: perfect(case, events[1:])))
    assert not report.passed
    topstep = next(r for r in report.results if r.case.firm == "topstep")
    assert topstep.runs[0].missing == [
        ("holiday_closure", "2026-01-01T00:00:00", "2026-01-01T23:59:00")
    ]
    markdown = report.to_markdown()
    assert "FAIL" in markdown and "missing holiday_closure 2026-01-01T00:00:00" in markdown
    payload = report.as_dict()
    assert payload["passed"] is False
    assert next(c for c in payload["cases"] if c["firm"] == "topstep")["passed"] is False


def test_an_invented_event_fails() -> None:
    def add(case, events):
        extra = {
            **events[0],
            "start_time": "2026-12-31T08:00:00",
            "end_time": "2026-12-31T09:00:00",
        }
        return perfect(case, [*events, extra])

    report, _ = run(only("blueberry-funded", add))
    assert not report.passed


def test_a_wrong_stated_offset_fails() -> None:
    """Topstep must leave the offset null: a fixed -06:00 is wrong for half the year."""

    def pin(case, events):
        return perfect(case, [{**e, "stated_utc_offset": "-06:00"} for e in events])

    report, _ = run(only("topstep", pin))
    topstep = next(r for r in report.results if r.case.firm == "topstep")
    assert topstep.runs[0].offset_mismatches
    assert not report.passed


def test_equivalent_offset_spellings_are_not_mismatches() -> None:
    assert _offset("+3") == _offset("GMT+03:00") == _offset("+03:00") == "+03:00"
    assert _offset(None) is None and _offset("") is None


def test_run_to_run_instability_is_reported() -> None:
    calls = {"n": 0}

    def flicker(case, events):
        if case.firm != "e8-markets":
            return perfect(case, events)
        calls["n"] += 1
        return perfect(case, events if calls["n"] % 2 else events[:-1])

    report, _ = run(flicker, runs=2)
    e8 = next(r for r in report.results if r.case.firm == "e8-markets")
    assert not e8.stable
    assert "| NO |" in report.to_markdown()


def test_consensus_is_exercised_as_in_production() -> None:
    report, backend = run(runs=1, consensus=3)
    assert report.passed and backend.calls == len(CASES) * 3


def test_a_backend_error_is_a_failed_run_not_a_crash() -> None:
    def boom(case, events):
        raise BackendError("quota exceeded")

    report, _ = run(only("ftmo", boom), runs=1)
    ftmo = next(r for r in report.results if r.case.firm == "ftmo")
    assert ftmo.runs[0].error and "quota exceeded" in ftmo.runs[0].error
    assert not report.passed


def test_lost_affected_text_and_unverified_evidence_are_reported_not_gated() -> None:
    """The live Topstep drift: right windows, no affected text. Visible, not red."""

    def drift(case, events):
        return [
            {**e, "affected": None, "evidence": "words the firm never wrote here"} for e in events
        ]

    report, _ = run(only("topstep", drift), runs=1)
    topstep = next(r for r in report.results if r.case.firm == "topstep")
    assert len(topstep.runs[0].affected_missing) == 13
    assert topstep.runs[0].evidence_verified == 0
    assert report.passed


# -- the CLI -----------------------------------------------------------------


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("[calendar]\nenabled = false\n[llm]\nconsensus_runs = 1\n", encoding="utf-8")
    return path


def test_the_eval_command_writes_reports_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "prop_firm_calendar.parsing.factory.make_backend", lambda cfg: FixtureBackend()
    )
    out_json, out_md = tmp_path / "eval.json", tmp_path / "eval.md"
    code = cli.main(
        [
            "--config", str(_config(tmp_path)), "eval", "--fixtures", str(FIXTURES),
            "--runs", "2", "--json", str(out_json), "--markdown", str(out_md),
        ]
    )  # fmt: skip
    assert code == cli.EXIT_OK
    assert json.loads(out_json.read_text(encoding="utf-8"))["passed"] is True
    assert "PASS" in out_md.read_text(encoding="utf-8")


def test_the_eval_command_exits_one_on_a_failing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = FixtureBackend(lambda case, events: [])
    monkeypatch.setattr("prop_firm_calendar.parsing.factory.make_backend", lambda cfg: broken)
    code = cli.main(
        [
            "--config",
            str(_config(tmp_path)),
            "eval",
            "--fixtures",
            str(FIXTURES),
            "--firm",
            "e8-markets",
        ]
    )
    assert code == cli.EXIT_ERROR
    assert broken.calls == 3  # --runs defaults to 3, one firm, consensus 1


def test_the_eval_command_needs_fixtures(tmp_path: Path) -> None:
    code = cli.main(["--config", str(_config(tmp_path)), "eval", "--fixtures", str(tmp_path / "x")])
    assert code == cli.EXIT_CONFIG
