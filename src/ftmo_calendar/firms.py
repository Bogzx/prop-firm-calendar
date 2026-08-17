"""Run every configured firm in one sync, isolating each firm's failures.

The single most important property here: **one firm's bad day must not cost
another firm's subscribers their calendar.** Before multi-firm support a scrape
failure aborted the run, which was correct when there was one source. With ten,
an unreachable site or a redesigned page would otherwise stop the other nine
from ever updating — the exact silent-staleness failure this project exists to
prevent, arrived at from the opposite direction.

So each firm is fetched, extracted and reconciled independently; a firm that
raises is recorded as unhealthy and the run continues. Only when *every* firm
fails does the run itself fail, which is what keeps the legacy single-firm
behaviour (raise, alert, exit non-zero) exactly as it was.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ftmo_calendar.config import AppConfig, FirmConfig
from ftmo_calendar.pipeline import RunReport, run_pipeline
from ftmo_calendar.sinks.base import EventSink
from ftmo_calendar.sources.factory import ResolvedFirm, resolve_firm
from ftmo_calendar.sources.politeness import stagger
from ftmo_calendar.state import State

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FirmOutcome:
    """How one firm's half of a sync went."""

    name: str
    display_name: str
    ok: bool
    error: str | None = None
    anomalies: tuple[str, ...] = ()
    posts_seen: int = 0
    posts_relevant: int = 0
    events_created: int = 0
    events_deleted: int = 0
    events_kept: int = 0

    def as_dict(self) -> dict:
        return {
            "firm": self.name,
            "display_name": self.display_name,
            "ok": self.ok,
            "status": "ok" if self.ok else ("error" if self.error else "anomaly"),
            "error": self.error,
            "anomalies": list(self.anomalies),
            "posts_seen": self.posts_seen,
            "posts_relevant": self.posts_relevant,
            "events_created": self.events_created,
            "events_deleted": self.events_deleted,
            "events_kept": self.events_kept,
        }


@dataclass
class MultiRunReport:
    """Aggregate of every firm's report, plus the per-firm breakdown."""

    reports: list[RunReport] = field(default_factory=list)
    outcomes: list[FirmOutcome] = field(default_factory=list)
    dry_run: bool = False

    @property
    def anomalies(self) -> list[str]:
        """Every firm's anomalies, already labelled with the firm they came from."""
        found: list[str] = []
        for outcome in self.outcomes:
            found.extend(outcome.anomalies)
            if outcome.error:
                found.append(f"{outcome.display_name}: sync failed — {outcome.error}")
        return found

    @property
    def created_lines(self) -> list[str]:
        return [line for r in self.reports for line in r.created_lines]

    @property
    def deleted_lines(self) -> list[str]:
        return [line for r in self.reports for line in r.deleted_lines]

    def totals(self) -> RunReport:
        """Everything summed into one report, for notifications and the CLI line."""
        total = RunReport(dry_run=self.dry_run)
        for r in self.reports:
            total.posts_seen += r.posts_seen
            total.posts_relevant += r.posts_relevant
            total.posts_skipped_unchanged += r.posts_skipped_unchanged
            total.events_created += r.events_created
            total.events_deleted += r.events_deleted
            total.events_kept += r.events_kept
            total.rejections += r.rejections
            total.created_lines.extend(r.created_lines)
            total.deleted_lines.extend(r.deleted_lines)
        total.anomalies = self.anomalies
        return total


class AllFirmsFailed(Exception):
    """Every configured firm raised. The run as a whole has failed."""


def run_firms(
    *,
    config: AppConfig,
    sink: EventSink,
    state: State,
    make_extractor,  # noqa: ANN001 - Callable[[ResolvedFirm], Extractor]
    dry_run: bool = False,
    now: datetime | None = None,
    resolve=resolve_firm,  # noqa: ANN001 - injected for tests
    stagger_fn=stagger,  # noqa: ANN001 - injected for tests
) -> MultiRunReport:
    """Sync every enabled firm; returns per-firm outcomes plus the aggregate."""
    now = now or datetime.now(UTC)
    result = MultiRunReport(dry_run=dry_run)
    firms = config.enabled_firms
    for index, firm_cfg in enumerate(firms):
        if index > 0 and not dry_run:
            # Spread the burst: N firms on one interval otherwise all fire on
            # the same second of every sync.
            stagger_fn(config.scrape.stagger_seconds)
        result_for_firm = _run_one(
            firm_cfg,
            config=config,
            sink=sink,
            state=state,
            make_extractor=make_extractor,
            dry_run=dry_run,
            now=now,
            resolve=resolve,
        )
        report, outcome = result_for_firm
        result.outcomes.append(outcome)
        if report is not None:
            result.reports.append(report)

    failures = [o for o in result.outcomes if o.error]
    if firms and len(failures) == len(firms):
        # Every source is down: that is a run failure, not a per-firm blemish,
        # and with one configured firm it is byte-for-byte the old behaviour.
        raise AllFirmsFailed("; ".join(f"{o.display_name}: {o.error}" for o in failures))
    return result


def _run_one(
    firm_cfg: FirmConfig,
    *,
    config: AppConfig,
    sink: EventSink,
    state: State,
    make_extractor,  # noqa: ANN001
    dry_run: bool,
    now: datetime,
    resolve,  # noqa: ANN001
) -> tuple[RunReport | None, FirmOutcome]:
    name = firm_cfg.profile
    try:
        resolved: ResolvedFirm = resolve(firm_cfg, config.scrape)
    except Exception as e:  # noqa: BLE001 - a bad profile must not kill other firms
        logger.exception("Could not build source for firm %s", name)
        return None, FirmOutcome(name=name, display_name=name, ok=False, error=str(e))

    try:
        report = run_pipeline(
            source=resolved.source,
            extractor=make_extractor(resolved),
            sink=sink,
            state=state,
            config=config,
            dry_run=dry_run,
            now=now,
            firm=resolved.name,
            display_name=resolved.display_name,
            source_timezone=resolved.timezone,
            keywords=resolved.keywords,
            require_stated_offset=resolved.profile.require_stated_offset,
        )
    except Exception as e:  # noqa: BLE001 - isolate this firm's failure
        logger.exception("Sync failed for firm %s", resolved.display_name)
        return None, FirmOutcome(
            name=resolved.name, display_name=resolved.display_name, ok=False, error=str(e)
        )

    return report, FirmOutcome(
        name=resolved.name,
        display_name=resolved.display_name,
        ok=not report.anomalies,
        anomalies=tuple(report.anomalies),
        posts_seen=report.posts_seen,
        posts_relevant=report.posts_relevant,
        events_created=report.events_created,
        events_deleted=report.events_deleted,
        events_kept=report.events_kept,
    )
