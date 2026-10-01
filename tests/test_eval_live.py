"""The same eval as `prop-firm-calendar eval`, as an opt-in pytest job.

Calls a real LLM API and costs money, so it never runs by default:

    PFC_LIVE_EVAL=1 LLM_API_KEY=... pytest -m live_llm [--config-file eval.toml]

PFC_EVAL_CONFIG points at a config.toml for the [llm] section (default: the
built-in Gemini defaults); PFC_EVAL_RUNS sets the repetitions (default 3).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(
        os.environ.get("PFC_LIVE_EVAL") != "1" or not os.environ.get("LLM_API_KEY"),
        reason="live LLM eval: set PFC_LIVE_EVAL=1 and LLM_API_KEY to run (real API calls)",
    ),
]

FIXTURES = Path(__file__).parent / "fixtures"


def test_the_configured_model_reproduces_every_golden_fixture(tmp_path: Path) -> None:
    from prop_firm_calendar.config import load_config
    from prop_firm_calendar.evaluation import discover, evaluate
    from prop_firm_calendar.parsing.factory import make_extractor_factory

    config = load_config(Path(os.environ.get("PFC_EVAL_CONFIG", tmp_path / "none.toml")))
    extractor_for = make_extractor_factory(config.llm)  # a [[llm.panel]] is evaluated too
    report = evaluate(
        discover(FIXTURES),
        lambda profile: extractor_for(profile.prompt_hints),
        runs=int(os.environ.get("PFC_EVAL_RUNS", "3")),
    )
    assert report.passed, report.to_markdown()
