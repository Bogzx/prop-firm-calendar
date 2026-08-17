from datetime import UTC, datetime
from pathlib import Path

import pytest

import ftmo_calendar.cli as cli
from ftmo_calendar.config import (
    AppConfig,
    CalendarConfig,
    EventRules,
    LLMConfig,
    NotifyConfig,
    SourceConfig,
)
from ftmo_calendar.firms import FirmOutcome, MultiRunReport
from ftmo_calendar.pipeline import RunReport
from ftmo_calendar.state import State

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


class RecordingNotifier:
    name = "recorder"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def notify_config(tmp_path: Path, **notify_kwargs) -> AppConfig:
    return AppConfig(
        source=SourceConfig(),
        llm=LLMConfig(api_key="test"),
        calendar=CalendarConfig(),
        events=EventRules(),
        base_dir=tmp_path,
        notify=NotifyConfig(**notify_kwargs),
    )


def test_run_is_default_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    def fake_run(config, dry_run):
        calls["dry_run"] = dry_run
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_cmd_run", fake_run)
    assert cli.main(["--config", str(tmp_path / "config.toml")]) == cli.EXIT_OK
    assert calls["dry_run"] is False


def test_dry_run_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}
    monkeypatch.setattr(
        cli, "_cmd_run", lambda config, dry_run: calls.update(dry_run=dry_run) or cli.EXIT_OK
    )
    cli.main(["--config", str(tmp_path / "config.toml"), "run", "--dry-run"])
    assert calls["dry_run"] is True


def test_config_error_exits_2(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[llm]\nprovider = "bogus"\n', encoding="utf-8")
    assert cli.main(["--config", str(tmp_path / "config.toml")]) == cli.EXIT_CONFIG


def test_pipeline_failure_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(config, dry_run):
        raise RuntimeError("scraper exploded")

    monkeypatch.setattr(cli, "_cmd_run", boom)
    assert cli.main(["--config", str(tmp_path / "config.toml")]) == cli.EXIT_ERROR


def test_status_with_no_state(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["--config", str(tmp_path / "config.toml"), "status"]) == cli.EXIT_OK
    assert "no runs recorded" in capsys.readouterr().out.lower()


def test_report_summary_format() -> None:
    report = RunReport(posts_seen=3, posts_relevant=2, events_created=1, dry_run=True)
    text = report.summary()
    assert "[dry-run]" in text and "1 created" in text


def test_failure_sends_error_notification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = RecordingNotifier()
    monkeypatch.setattr(cli, "make_notifiers", lambda cfg: [recorder])

    def boom(config, dry_run):
        raise RuntimeError("scraper exploded")

    monkeypatch.setattr(cli, "_cmd_run", boom)
    assert cli.main(["--config", str(tmp_path / "config.toml")]) == cli.EXIT_ERROR
    assert len(recorder.sent) == 1
    assert "scraper exploded" in recorder.sent[0]


def test_notify_run_outcome_sends_on_changes(tmp_path: Path) -> None:
    recorder = RecordingNotifier()
    report = RunReport(events_created=1)
    report.created_lines.append("⚠️ Maintenance — Sat 06 Jun 08:00–14:00")
    cli._notify_run_outcome(notify_config(tmp_path), [recorder], report, State(), now=NOW)
    assert len(recorder.sent) == 1
    assert "Maintenance" in recorder.sent[0]


def test_notify_run_outcome_quiet_when_no_changes(tmp_path: Path) -> None:
    recorder = RecordingNotifier()
    cli._notify_run_outcome(notify_config(tmp_path), [recorder], RunReport(), State(), now=NOW)
    assert recorder.sent == []


def test_heartbeat_sent_when_due_and_stamped(tmp_path: Path) -> None:
    recorder = RecordingNotifier()
    config = notify_config(tmp_path, heartbeat_hours=24)
    state = State(last_heartbeat="2026-06-07T00:00:00+00:00")  # >24h ago
    cli._notify_run_outcome(config, [recorder], RunReport(), state, now=NOW)
    assert len(recorder.sent) == 1 and "✅" in recorder.sent[0]
    assert state.last_heartbeat == NOW.isoformat()


def test_heartbeat_not_sent_when_fresh(tmp_path: Path) -> None:
    recorder = RecordingNotifier()
    config = notify_config(tmp_path, heartbeat_hours=24)
    fresh = "2026-06-09T06:00:00+00:00"  # 6h ago
    state = State(last_heartbeat=fresh)
    cli._notify_run_outcome(config, [recorder], RunReport(), state, now=NOW)
    assert recorder.sent == []
    assert state.last_heartbeat == fresh


# -- anomalies are not "success" ------------------------------------------


def anomaly_report() -> RunReport:
    report = RunReport(posts_seen=4, posts_relevant=0)
    report.anomalies.append("keyword gate matched none of 4 scraped post(s)")
    return report


def test_anomaly_notification_is_sent_even_with_no_calendar_changes(tmp_path: Path) -> None:
    """The quiet failure: nothing changed *because* nothing was recognised."""
    recorder = RecordingNotifier()
    cli._notify_run_outcome(notify_config(tmp_path), [recorder], anomaly_report(), State(), now=NOW)
    assert len(recorder.sent) == 1
    assert "keyword gate" in recorder.sent[0]


def test_anomaly_notification_can_be_turned_off(tmp_path: Path) -> None:
    recorder = RecordingNotifier()
    config = notify_config(tmp_path, on_anomalies=False)
    cli._notify_run_outcome(config, [recorder], anomaly_report(), State(), now=NOW)
    assert recorder.sent == []


def _sync_result(report: RunReport) -> MultiRunReport:
    """Wrap one firm's report the way _run_sync now returns it."""
    return MultiRunReport(
        reports=[report],
        outcomes=[
            FirmOutcome(
                name="ftmo",
                display_name="FTMO",
                ok=not report.anomalies,
                anomalies=tuple(report.anomalies),
            )
        ],
    )


def test_run_exits_nonzero_on_anomaly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit codes are the documented contract for cron and systemd."""
    monkeypatch.setattr(cli, "_run_sync", lambda config, dry_run: _sync_result(anomaly_report()))
    assert cli.main(["--config", str(tmp_path / "config.toml")]) == cli.EXIT_ERROR


def test_run_exits_zero_on_a_clean_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "_run_sync",
        lambda config, dry_run: _sync_result(RunReport(posts_seen=4, posts_relevant=4)),
    )
    assert cli.main(["--config", str(tmp_path / "config.toml")]) == cli.EXIT_OK
