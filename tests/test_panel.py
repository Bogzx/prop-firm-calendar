"""The model panel: several independent models, one vote each, a quorum must agree.

Every backend here is scripted; nothing calls a real model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import prop_firm_calendar.parsing.factory as factory
from prop_firm_calendar.config import (
    AppConfig,
    CalendarConfig,
    ConfigError,
    EventRules,
    LLMConfig,
    SourceConfig,
    load_config,
)
from prop_firm_calendar.evaluation import Case, discover, evaluate
from prop_firm_calendar.models import SourcePost, TradingEvent
from prop_firm_calendar.parsing.llm import (
    BackendError,
    EventExtractor,
    ExtractionError,
    Juror,
    PanelExtractor,
)
from prop_firm_calendar.pipeline import run_pipeline
from prop_firm_calendar.state import State


class Scripted:
    """Answers every call with the same reply, or raises it."""

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.models: list[str] = []

    def complete(self, prompt: str, model: str) -> str:
        self.models.append(model)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def event(start: str, end: str, **extra: str) -> dict:
    return {"event_type": "maintenance", "start_time": start, "end_time": end, **extra}


SAT = event("2026-06-06T08:00:00", "2026-06-06T14:00:00", stated_utc_offset="+03:00")
SUN = event("2026-06-07T08:00:00", "2026-06-07T10:00:00", stated_utc_offset="+03:00")


def reply(*events: dict) -> str:
    return json.dumps(list(events))


def panel(*replies: str | Exception, quorum: int = 0) -> PanelExtractor:
    jurors = [Juror(f"m{i}", Scripted(r), f"model-{i}") for i, r in enumerate(replies, 1)]
    return PanelExtractor(jurors, quorum)


# -- voting ------------------------------------------------------------------


def test_an_event_needs_a_majority_of_the_panel() -> None:
    extractor = panel(reply(SAT, SUN), reply(SAT), reply(SAT))
    [kept] = extractor.extract("text")
    assert kept.start_time == SAT["start_time"]
    [dispute] = extractor.last_disputes
    assert dispute.event.start_time == SUN["start_time"]
    assert dispute.voters == ("m1",)
    assert dispute.describe() == (
        "only 1 of 3 models extracted it (quorum 2): m1 did; m2, m3 did not"
    )


def test_each_model_is_asked_once_with_its_own_model_id() -> None:
    extractor = panel(reply(SAT), reply(SAT), reply(SAT))
    extractor.extract("text")
    assert [j.backend.models for j in extractor.jurors] == [  # type: ignore[attr-defined]
        ["model-1"],
        ["model-2"],
        ["model-3"],
    ]


def test_timestamps_formatted_differently_still_agree() -> None:
    """Different vendors format times differently; the instant is what is compared."""
    short = {**SAT, "start_time": "2026-06-06T08:00", "end_time": "2026-06-06T14:00"}
    extractor = panel(reply(SAT), reply(short), reply())
    assert len(extractor.extract("text")) == 1
    assert extractor.last_disputes == []


def test_the_merge_keeps_the_most_explicit_reading() -> None:
    bare = {**SAT, "stated_utc_offset": None}
    detailed = {**SAT, "affected": "MT4, MT5", "evidence": "Saturday 6 June 08:00 to 14:00"}
    [kept] = panel(reply(bare), reply(detailed), reply(bare)).extract("text")
    assert kept.affected == "MT4, MT5"
    assert kept.evidence == "Saturday 6 June 08:00 to 14:00"


def test_a_failed_model_abstains_and_counts_against_every_event() -> None:
    """2 of 3 must agree whoever is down: an outage never lowers the bar."""
    extractor = panel(BackendError("quota"), reply(SAT, SUN), reply(SAT))
    [kept] = extractor.extract("text")
    assert kept.start_time == SAT["start_time"]
    assert [d.event.start_time for d in extractor.last_disputes] == [SUN["start_time"]]
    assert extractor.last_ballots[0].events is None
    assert "quota" in extractor.last_ballots[0].error


def test_fewer_answers_than_the_quorum_fails_instead_of_reporting_nothing() -> None:
    extractor = panel(BackendError("down"), BackendError("timeout"), reply(SAT))
    with pytest.raises(ExtractionError, match="only 1 of 3 panel models answered"):
        extractor.extract("text")
    # Kept for the eval harness, which scores each member even then.
    assert [b.juror for b in extractor.last_ballots] == ["m1", "m2", "m3"]


def test_an_explicit_quorum_can_demand_unanimity() -> None:
    assert panel(reply(SAT), reply(SAT), reply(), quorum=3).extract("text") == []
    assert len(panel(reply(SAT), reply(SAT), reply(SAT), quorum=3).extract("text")) == 1


def test_an_empty_answer_is_a_vote_not_an_abstention() -> None:
    extractor = panel(reply(), reply(), reply(SAT))
    assert extractor.extract("text") == []
    assert len(extractor.last_disputes) == 1


@pytest.mark.parametrize(
    ("jurors", "quorum", "message"),
    [
        (1, 0, "at least two"),
        (3, 4, "between 1 and 3"),
    ],
)
def test_impossible_panels_are_refused(jurors: int, quorum: int, message: str) -> None:
    members = [Juror(f"m{i}", Scripted(reply()), "x") for i in range(jurors)]
    with pytest.raises(ValueError, match=message):
        PanelExtractor(members, quorum)


def test_member_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        PanelExtractor([Juror("a", Scripted(reply()), "x"), Juror("a", Scripted(reply()), "y")])


def test_the_single_model_default_is_unchanged() -> None:
    """Without a panel, repeated runs still compare the model's strings verbatim."""
    short = {**SAT, "start_time": "2026-06-06T08:00", "end_time": "2026-06-06T14:00"}

    class Runs:
        def __init__(self) -> None:
            self.replies = [reply(SAT), reply(short), reply(short)]

        def complete(self, prompt: str, model: str) -> str:
            return self.replies.pop(0)

    [kept] = EventExtractor(Runs(), ["m"], consensus_runs=3).extract("text")
    assert kept.start_time == "2026-06-06T08:00"  # 2 of 3 runs, exactly as before


# -- configuration -----------------------------------------------------------


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


PANEL_TOML = """
[llm]
provider = "openai-compatible"
base_url = "https://openrouter.ai/api/v1"
panel_quorum = 2

[[llm.panel]]
model = "deepseek/deepseek-chat"

[[llm.panel]]
model = "gemini-2.5-flash"
provider = "gemini"
api_key_env = "GEMINI_PANEL_KEY"

[[llm.panel]]
model = "gpt-5-mini"
name = "openai"
provider = "openai-compatible"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
"""


def test_a_panel_loads_from_toml_with_keys_from_the_environment(tmp_path: Path) -> None:
    env = {"LLM_API_KEY": "k-router", "GEMINI_PANEL_KEY": "k-gem", "OPENAI_API_KEY": "k-oai"}
    llm = load_config(write(tmp_path, PANEL_TOML), env=env).llm
    first, second, third = llm.panel
    # No provider of its own: inherits [llm]'s provider, URL and key.
    assert (first.provider, first.base_url, first.api_key) == (
        "openai-compatible",
        "https://openrouter.ai/api/v1",
        "k-router",
    )
    # Its own provider: [llm] base_url belongs to someone else and is not inherited.
    assert (second.provider, second.base_url, second.api_key) == ("gemini", "", "k-gem")
    assert (third.label, third.base_url, third.api_key) == (
        "openai",
        "https://api.openai.com/v1",
        "k-oai",
    )
    assert llm.quorum == 2


def test_without_a_panel_nothing_changes(tmp_path: Path) -> None:
    llm = load_config(write(tmp_path, "[llm]\nconsensus_runs = 3\n"), env={}).llm
    assert llm.panel == ()
    assert factory.describe_extraction(llm) == {
        "mode": "consensus",
        "models": ["gemini-2.5-flash", "gemini-2.0-flash"],
        "runs": 3,
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[[llm.panel]]\nmodel = "a"\napi_key = "sk-123"\n[[llm.panel]]\nmodel = "b"\n',
         "no api_key"),
        ('[[llm.panel]]\nmodel = "a"\n', "at least two models"),
        ('[[llm.panel]]\nmodel = "a"\n[[llm.panel]]\nmodel = "a"\n', "same model twice"),
        ('[[llm.panel]]\nprovider = "gemini"\n[[llm.panel]]\nmodel = "b"\n', "needs a 'model'"),
        ('[[llm.panel]]\nmodel = "a"\nprovider = "anthropic"\n[[llm.panel]]\nmodel = "b"\n',
         "unknown provider"),
        ('[[llm.panel]]\nmodel = "a"\ntemperature = 1\n[[llm.panel]]\nmodel = "b"\n',
         "invalid \\[\\[llm.panel\\]\\] entry"),
        ('[llm]\npanel_quorum = 3\n[[llm.panel]]\nmodel = "a"\n[[llm.panel]]\nmodel = "b"\n',
         "between 1 and 2"),
        ("[llm]\npanel_quorum = 2\n", "no \\[\\[llm.panel\\]\\] models"),
    ],
)  # fmt: skip
def test_bad_panels_are_configuration_errors(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, body), env={})


# -- construction ------------------------------------------------------------


class FakeClient:
    def __init__(self, provider: str, api_key: str, base_url: str, timeout: float) -> None:
        self.endpoint = (provider, api_key, base_url, timeout)

    def complete(self, prompt: str, model: str) -> str:
        return "[]"


@pytest.fixture
def fake_clients(monkeypatch: pytest.MonkeyPatch) -> list[FakeClient]:
    made: list[FakeClient] = []

    def build(provider: str, api_key: str, base_url: str, timeout: float) -> FakeClient:
        made.append(FakeClient(provider, api_key, base_url, timeout))
        return made[-1]

    monkeypatch.setattr(factory, "_backend", build)
    return made


def test_the_factory_builds_a_panel_and_shares_clients(
    tmp_path: Path, fake_clients: list[FakeClient]
) -> None:
    body = PANEL_TOML.replace('model = "gemini-2.5-flash"\nprovider = "gemini"\n', 'model = "x"\n')
    body = body.replace('api_key_env = "GEMINI_PANEL_KEY"\n', "")
    env = {"LLM_API_KEY": "k-router", "OPENAI_API_KEY": "k-oai"}
    llm = load_config(write(tmp_path, body), env=env).llm
    extractor = factory.make_extractor_factory(llm)("hints")
    assert isinstance(extractor, PanelExtractor)
    assert [(j.name, j.model) for j in extractor.jurors] == [
        ("deepseek/deepseek-chat", "deepseek/deepseek-chat"),
        ("x", "x"),
        ("openai", "gpt-5-mini"),
    ]
    assert len(fake_clients) == 2  # two models behind one OpenRouter key share a client
    assert extractor.quorum == 2
    assert extractor.prompt_hints == "hints"
    assert factory.calls_per_extraction(llm) == 3
    assert factory.describe_extraction(llm) == {
        "mode": "panel",
        "models": ["deepseek/deepseek-chat", "x", "openai"],
        "quorum": 2,
    }


def test_a_member_without_its_key_names_the_variable(
    tmp_path: Path, fake_clients: list[FakeClient]
) -> None:
    llm = load_config(write(tmp_path, PANEL_TOML), env={"LLM_API_KEY": "k"}).llm
    with pytest.raises(ConfigError, match="GEMINI_PANEL_KEY"):
        factory.make_extractor_factory(llm)


def test_the_default_factory_is_the_single_model_extractor(fake_clients: list[FakeClient]) -> None:
    llm = LLMConfig(api_key="k", models=("a", "b"), consensus_runs=3)
    extractor = factory.make_extractor_factory(llm)("")
    assert isinstance(extractor, EventExtractor)
    assert (extractor.models, extractor.consensus_runs) == (["a", "b"], 3)
    assert factory.calls_per_extraction(llm) == 3


# -- in the pipeline ---------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
POST = SourcePost(
    post_key="trading-update-2026-06-04",
    title="Trading Update | Jun 4 2026",
    text="maintenance on Saturday 6 Jun 2026 08:00 to 14:00 GMT+3",
    url="https://ftmo.com/en/trading-updates/",
)


class OnePost:
    def fetch(self) -> list[SourcePost]:
        return [POST]


class RecordingSink:
    def __init__(self) -> None:
        self.created: list[TradingEvent] = []

    def find_event_id_by_key(self, event_key: str) -> str | None:
        return None

    def create_event(self, event: TradingEvent) -> str:
        self.created.append(event)
        return f"gid{len(self.created)}"

    def delete_event(self, event_id: str) -> None:
        raise AssertionError("nothing should be deleted")


def run_with(tmp_path: Path, extractor: PanelExtractor):
    state = State()
    sink = RecordingSink()
    config = AppConfig(
        source=SourceConfig(),
        llm=LLMConfig(api_key="test"),
        calendar=CalendarConfig(),
        events=EventRules(),
        base_dir=tmp_path,
    )
    report = run_pipeline(
        source=OnePost(),
        extractor=extractor,
        sink=sink,
        state=state,
        config=config,
        now=NOW,
    )
    return report, sink, state


def test_a_disputed_upcoming_window_is_not_published_but_is_loud(tmp_path: Path) -> None:
    report, sink, state = run_with(tmp_path, panel(reply(SAT, SUN), reply(SAT), reply(SAT)))
    assert [e.start.isoformat() for e in sink.created] == ["2026-06-06T08:00:00+03:00"]
    [anomaly] = report.anomalies
    assert "only 1 of 3 models extracted it" in anomaly
    assert state.posts[POST.post_key].rejected == [
        "maintenance 2026-06-07T08:00:00: only 1 of 3 models extracted it (quorum 2): "
        "m1 did; m2, m3 did not"
    ]


def test_a_disputed_window_that_already_ended_is_not_an_alarm(tmp_path: Path) -> None:
    past = event("2026-05-01T08:00:00", "2026-05-01T10:00:00", stated_utc_offset="+03:00")
    report, sink, _ = run_with(tmp_path, panel(reply(SAT, past), reply(SAT), reply(SAT)))
    assert len(sink.created) == 1
    assert report.anomalies == []


def test_a_panel_that_cannot_reach_quorum_fails_the_firm(tmp_path: Path) -> None:
    extractor = panel(BackendError("a"), BackendError("b"), reply(SAT))
    with pytest.raises(ExtractionError):
        run_with(tmp_path, extractor)


# -- in the eval -------------------------------------------------------------


FIXTURES = Path(__file__).parent / "fixtures"
CASES = discover(FIXTURES)


class FixtureModel:
    """Answers with the fixture's expected events; `drop` loses one per post."""

    def __init__(self, drop: bool = False) -> None:
        self.drop = drop

    def complete(self, prompt: str, model: str) -> str:
        case: Case = next(c for c in CASES if c.post.text in prompt)
        quote = " ".join(case.post.text.split()[:8])
        events = [{**e.model_dump(), "evidence": quote} for e in case.expected]
        return json.dumps(events[1:] if self.drop else events)


def test_the_eval_scores_each_member_alone_at_no_extra_cost() -> None:
    jurors = [
        Juror("good-1", FixtureModel(), "a"),
        Juror("sloppy", FixtureModel(drop=True), "b"),
        Juror("good-2", FixtureModel(), "c"),
    ]
    report = evaluate(CASES, lambda profile: PanelExtractor(jurors), runs=1)
    assert report.passed  # the quorum outvotes the member that lost an event
    jurors_by_case = [r.as_dict(0, 0)["jurors"] for r in report.results]
    assert all(j["sloppy"]["passed"] is False for j in jurors_by_case)
    assert all(j["good-1"]["passed"] and j["good-2"]["passed"] for j in jurors_by_case)
    markdown = report.to_markdown()
    assert "### Panel members scored alone" in markdown
    assert "| sloppy | 7 | 1 | 0 | 0 | 0 | fail |" in markdown  # ftmo: 8 expected


def test_a_single_model_eval_has_no_member_table() -> None:
    report = evaluate(CASES, lambda p: EventExtractor(FixtureModel(), ["m"]), runs=1)
    assert "Panel members" not in report.to_markdown()
    assert all(r.as_dict(0, 0)["jurors"] == {} for r in report.results)


class ByModel:
    """One client for every member: answers well unless the model is the sloppy one."""

    def complete(self, prompt: str, model: str) -> str:
        return FixtureModel(drop=model == "sloppy-model").complete(prompt, model)


def test_the_eval_command_runs_a_configured_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import prop_firm_calendar.cli as cli

    monkeypatch.setattr(factory, "_backend", lambda *args: ByModel())
    monkeypatch.setenv("LLM_API_KEY", "k")
    config = write(
        tmp_path,
        '[calendar]\nenabled = false\n[llm]\nprovider = "openai-compatible"\n'
        '[[llm.panel]]\nmodel = "a"\n[[llm.panel]]\nmodel = "sloppy-model"\n'
        '[[llm.panel]]\nmodel = "c"\n',
    )
    out = tmp_path / "eval.md"
    code = cli.main(
        ["--config", str(config), "eval", "--fixtures", str(FIXTURES), "--runs", "1",
         "--markdown", str(out)]
    )  # fmt: skip
    assert code == cli.EXIT_OK
    markdown = out.read_text(encoding="utf-8")
    assert "## Extraction eval: PASS" in markdown
    assert "| ftmo | trading-update-21-may-2026 | sloppy-model | 7 | 1 | 0 | 0 | 0 | fail |" in (
        markdown
    )


@pytest.mark.parametrize(
    "member",
    [
        'model = "g"\nprovider = "gemini"\n',  # another vendor
        'model = "o"\nbase_url = "https://api.openai.com/v1"\n',  # another endpoint
        'model = "o"\nprovider = "openai-compatible"\n',  # own provider: [llm] URL not inherited
    ],
)
def test_a_member_with_its_own_endpoint_must_name_its_key(tmp_path: Path, member: str) -> None:
    """Otherwise it silently gets [llm]'s key, every call is refused, and it abstains."""
    body = (
        '[llm]\nprovider = "openai-compatible"\nbase_url = "https://openrouter.ai/api/v1"\n'
        f'[[llm.panel]]\nmodel = "a"\n[[llm.panel]]\n{member}'
    )
    with pytest.raises(ConfigError, match="set api_key_env"):
        load_config(write(tmp_path, body), env={"LLM_API_KEY": "k"})


def test_sharing_the_llm_key_on_purpose_is_allowed(tmp_path: Path) -> None:
    body = (
        '[llm]\nprovider = "openai-compatible"\nbase_url = "https://openrouter.ai/api/v1"\n'
        '[[llm.panel]]\nmodel = "a"\n'
        '[[llm.panel]]\nmodel = "b"\nbase_url = "https://openrouter.ai/api/v1/"\n'
        'api_key_env = "LLM_API_KEY"\n'
    )
    first, second = load_config(write(tmp_path, body), env={"LLM_API_KEY": "k"}).llm.panel
    assert (first.api_key, second.api_key) == ("k", "k")
    assert first.api_key_env == ""  # inherited, so no variable of its own to name
