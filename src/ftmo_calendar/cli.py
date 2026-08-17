"""Command-line interface: ftmo-calendar run|auth|status|serve."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from ftmo_calendar import __version__
from ftmo_calendar.config import AppConfig, ConfigError, load_config
from ftmo_calendar.firms import MultiRunReport
from ftmo_calendar.notify.base import (
    EventPayload,
    Notifier,
    format_anomaly_message,
    format_error_message,
    format_heartbeat_message,
    format_run_message,
    notify_all,
)
from ftmo_calendar.notify.factory import make_notifiers
from ftmo_calendar.pipeline import RunReport
from ftmo_calendar.state import State

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ftmo-calendar",
        description="Sync FTMO trading updates (maintenance, closures) to Google Calendar.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="path to config.toml (default: ./config.toml)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="check FTMO and sync the calendar (default)")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show planned changes without touching the calendar or state",
    )

    auth_parser = subparsers.add_parser("auth", help="interactive Google OAuth authorization")
    auth_parser.add_argument(
        "--check", action="store_true", help="report credential status without authorizing"
    )

    subparsers.add_parser("status", help="show tracked posts and events from the last runs")

    serve_parser = subparsers.add_parser(
        "serve", help="run periodic syncs and host the ICS feed + status page over HTTP"
    )
    serve_parser.add_argument("--port", type=int, default=None, help="override [serve] port")
    return parser


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stderr,
    )


def _notify_run_outcome(
    config: AppConfig,
    notifiers: list[Notifier],
    report: RunReport,
    state: State,
    now: datetime | None = None,
) -> None:
    """Send the change message and, when due, a heartbeat; stamps the heartbeat in state."""
    if config.notify.on_events:
        message = format_run_message(report)
        if message:
            notify_all(notifiers, message, EventPayload.from_report(report))
    if config.notify.on_anomalies:
        # A run that exits 0 with a broken keyword gate or a collapsed
        # extraction is the failure mode this project is meant not to have.
        anomaly_message = format_anomaly_message(report)
        if anomaly_message:
            notify_all(notifiers, anomaly_message, EventPayload.from_report(report))
    hours = config.notify.heartbeat_hours
    if not hours or not notifiers:
        return
    now = now or datetime.now(UTC)
    last = state.last_heartbeat
    if last is not None:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        if now - last_dt < timedelta(hours=hours):
            return
    notify_all(notifiers, format_heartbeat_message(report))
    state.last_heartbeat = now.isoformat()


def _build_sink(config: AppConfig, dry_run: bool):  # noqa: ANN202
    """Google credentials are only touched for a real Google-bound run.

    Dry runs never call the sink (reconcile short-circuits first), and
    feed-only mode (`[calendar] enabled = false`) has no calendar at all —
    neither should require any Google setup.
    """
    if dry_run or not config.calendar.enabled:
        from ftmo_calendar.sinks.null import StateOnlySink

        if not config.calendar.enabled:
            logger.info("Calendar sync disabled — feed-only mode")
        return StateOnlySink()
    from ftmo_calendar.sinks.auth import load_credentials
    from ftmo_calendar.sinks.google_calendar import GoogleCalendarSink

    credentials = load_credentials(config.calendar, config.base_dir)
    return GoogleCalendarSink(credentials, config.calendar)


def _firm_titles(config: AppConfig) -> dict[str, str]:
    """profile name -> display name, for naming feeds and the status page."""
    from ftmo_calendar.sources.profile import load_profile

    titles: dict[str, str] = {}
    for firm in config.firms:
        try:
            profile = load_profile(firm.profile)
        except ConfigError:  # pragma: no cover - load_config already validated
            continue
        titles[profile.name] = profile.display_name
    return titles


def _default_firm(config: AppConfig) -> str:
    """Which firm owns state entries written before per-firm tracking.

    The first configured firm, because that is exactly what the single
    `[source]` scraper was when those entries were written. See State.firm_of.
    """
    return config.firms[0].profile if config.firms else ""


def _write_feed(config: AppConfig, state: State) -> None:
    from ftmo_calendar.sinks.ics import write_ics

    write_ics(
        state,
        config.resolve(config.ics.path),
        config.calendar.reminders_minutes,
        source_url=config.source.url,
        refresh_minutes=config.serve.sync_interval_minutes,
        default_firm=_default_firm(config),
        firm_titles=_firm_titles(config),
        tz_name=config.calendar.timezone,
    )


def _run_sync(config: AppConfig, dry_run: bool) -> MultiRunReport:
    """One full sync across every configured firm.

    Returns the multi-firm report so callers can react to anomalies and expose
    per-firm health. With a single firm configured — which is what a config
    predating `[[firms]]` produces — this is the previous behaviour: one
    pipeline run, and a scrape failure still propagates out of here.
    """
    from ftmo_calendar.firms import run_firms
    from ftmo_calendar.parsing.factory import make_backend
    from ftmo_calendar.parsing.llm import EventExtractor
    from ftmo_calendar.state import load_state, save_state

    if not config.calendar.enabled:
        # Without Google, the ICS feed is the only output — force it on.
        config = dataclasses.replace(config, ics=dataclasses.replace(config.ics, enabled=True))

    backend = make_backend(config.llm)

    def make_extractor(resolved) -> EventExtractor:  # noqa: ANN001 - ResolvedFirm
        # Prompt hints are per firm: house vocabulary and the boilerplate that
        # firm repeats in every post.
        return EventExtractor(
            backend,
            config.llm.models,
            consensus_runs=config.llm.consensus_runs,
            prompt_hints=resolved.profile.prompt_hints,
        )

    sink = _build_sink(config, dry_run)
    state = load_state(config.state_path)

    result = run_firms(
        config=config,
        sink=sink,
        state=state,
        make_extractor=make_extractor,
        dry_run=dry_run,
    )
    totals = result.totals()
    if not dry_run:
        _notify_run_outcome(config, make_notifiers(config.notify), totals, state)
        save_state(state, config.state_path)
        if config.ics.enabled:
            _write_feed(config, state)
    for report in result.reports:
        print(report.summary())
    if len(result.reports) != 1:
        print(totals.summary())
    return result


def _cmd_run(config: AppConfig, dry_run: bool) -> int:
    report = _run_sync(config, dry_run).totals()
    # An anomaly means the run "succeeded" while producing a result we do not
    # believe. Exiting non-zero is what makes cron, systemd and the README's
    # documented exit codes able to notice it.
    return EXIT_ERROR if report.anomalies else EXIT_OK


def _cmd_auth(config: AppConfig, check: bool) -> int:
    from ftmo_calendar.sinks.auth import describe_credentials, interactive_auth

    if not config.calendar.enabled:
        print("Calendar sync is disabled ([calendar] enabled = false) — no Google auth needed.")
        return EXIT_OK
    if check:
        print(describe_credentials(config.calendar, config.base_dir))
        return EXIT_OK
    if config.calendar.auth_mode == "service_account":
        print("auth_mode is 'service_account' — no interactive authorization needed.")
        print(describe_credentials(config.calendar, config.base_dir))
        return EXIT_OK
    token_path = interactive_auth(config.calendar, config.base_dir)
    print(f"Authorized. Token saved to {token_path}.")
    return EXIT_OK


def _cmd_serve(config: AppConfig, port_override: int | None) -> int:
    from ftmo_calendar.server import FeedSelection, check_writable, serve_forever

    # Before anything else: if the data directory is not writable, nothing this
    # process does will ever be saved. Say so now, not after hours of a
    # container that looks perfectly healthy.
    check_writable(config.base_dir)

    # The feed is the point of serve mode — force ICS generation on.
    config = dataclasses.replace(config, ics=dataclasses.replace(config.ics, enabled=True))

    # Serve last-good data immediately: the feed must not 404 after a restart
    # just because the most recent sync attempt failed.
    from ftmo_calendar.state import load_state

    existing_state = load_state(config.state_path)
    if existing_state.posts:
        _write_feed(config, existing_state)

    def sync() -> MultiRunReport:
        # The whole report travels back to ServerStatus: anomalies turn
        # /healthz 503, and the per-firm outcomes keep each source's health
        # individually visible instead of averaged into one badge.
        return _run_sync(config, dry_run=False)

    titles = _firm_titles(config)
    default_firm = _default_firm(config)

    def feed_renderer(selection: FeedSelection) -> bytes:
        from ftmo_calendar.sinks.ics import render_ics
        from ftmo_calendar.state import load_state

        return render_ics(
            load_state(config.state_path),
            config.calendar.reminders_minutes,
            source_url=config.source.url,
            refresh_minutes=config.serve.sync_interval_minutes,
            types=selection.types,
            firms=selection.firms,
            default_firm=default_firm,
            firm_titles=titles,
            tz_name=config.calendar.timezone,
        ).encode("utf-8")

    from ftmo_calendar.stats import StatsStore

    names = [f.profile for f in config.enabled_firms]
    return serve_forever(
        host=config.serve.host,
        port=port_override or config.serve.port,
        interval_seconds=config.serve.sync_interval_minutes * 60,
        ics_path=config.resolve(config.ics.path),
        state_path=config.state_path,
        sync_fn=sync,
        on_error=lambda e: _notify_failure(config, "run", e),
        feed_renderer=feed_renderer,
        stats=StatsStore(config.base_dir / "stats.json"),
        source_name=", ".join(titles.get(n, n) for n in names) or "FTMO",
        valid_firms=names,
    )


def _cmd_status(config: AppConfig) -> int:
    from ftmo_calendar.state import load_state

    state = load_state(config.state_path)
    if not state.posts:
        print("No runs recorded yet (state file empty or missing).")
        return EXIT_OK
    print(f"Tracked posts ({len(state.posts)}):")
    for key, post in sorted(state.posts.items(), reverse=True):
        print(f"  {key}  last seen {post.last_seen}  events: {len(post.events)}")
        for event in post.events:
            print(f"    - {event.event_key}  ends {event.end}  (google id {event.google_event_id})")
    return EXIT_OK


def _force_utf8_streams() -> None:
    """Event summaries contain emoji; Windows pipes default to cp1252 and would crash."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    config_path: Path = args.config
    load_dotenv(config_path.resolve().parent / ".env")

    try:
        config = load_config(config_path)
    except ConfigError as e:
        logger.error("Configuration error: %s", e)
        return EXIT_CONFIG

    from ftmo_calendar.sinks.auth import AuthError

    command = args.command or "run"
    try:
        if command == "run":
            return _cmd_run(config, dry_run=getattr(args, "dry_run", False))
        if command == "auth":
            return _cmd_auth(config, check=args.check)
        if command == "serve":
            return _cmd_serve(config, port_override=args.port)
        return _cmd_status(config)
    except (AuthError, ConfigError) as e:
        logger.error("%s", e)
        _notify_failure(config, command, e)
        return EXIT_CONFIG
    except Exception as e:
        logger.exception("Run failed")
        _notify_failure(config, command, e)
        return EXIT_ERROR


def _notify_failure(config: AppConfig, command: str, error: BaseException) -> None:
    """The tool's core promise: it never fails silently. Only `run` failures alert."""
    if command == "run" and config.notify.on_errors:
        notify_all(make_notifiers(config.notify), format_error_message(error))


def entry() -> None:
    sys.exit(main())
