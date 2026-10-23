"""Machine-verifiable consecutive trading-day stability evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cnequity.orchestrator.manifest import Manifest
from cnequity.storage.atomic import write_json_atomic


@dataclass(frozen=True)
class StabilityDay:
    trade_date: str
    run_id: str | None
    status: str
    dataset_results: int
    passed: bool
    reason: str


@dataclass(frozen=True)
class StabilityReport:
    generated_at: str
    job_name: str
    required_days: int
    calendar_days_available: int
    consecutive_passed: int
    passed: bool
    days: tuple[StabilityDay, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "job_name": self.job_name,
            "required_days": self.required_days,
            "calendar_days_available": self.calendar_days_available,
            "consecutive_passed": self.consecutive_passed,
            "passed": self.passed,
            "days": [asdict(item) for item in self.days],
        }


def _run_trade_date(run: Any) -> date | None:
    try:
        metadata = json.loads(run["metadata_json"] or "{}")
        value = metadata.get("trade_date")
        return date.fromisoformat(value) if isinstance(value, str) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _latest_runs_by_date(manifest: Manifest, job_name: str) -> dict[date, Any]:
    """Select the newest attempt for each logical trade date."""
    selected: dict[date, Any] = {}
    for run in manifest.list_runs(job_name):
        trade_date = _run_trade_date(run)
        if trade_date is None:
            continue
        current = selected.get(trade_date)
        if current is None or str(run["started_at"]) > str(current["started_at"]):
            selected[trade_date] = run
    return selected


def _evaluate_run(manifest: Manifest, trade_date: date, run: Any | None) -> StabilityDay:
    if run is None:
        return StabilityDay(
            trade_date=trade_date.isoformat(),
            run_id=None,
            status="missing",
            dataset_results=0,
            passed=False,
            reason="no run receipt for trading day",
        )

    run_id = str(run["run_id"])
    status = str(run["status"])
    aggregate = manifest.aggregate_run_status(run_id)
    result_count = len(aggregate["results"])
    if aggregate["core_failures"]:
        passed = False
        reason = "core dataset stage failed or was blocked"
    elif status == "success":
        passed = True
        reason = "run succeeded"
    elif status == "degraded" and result_count:
        passed = True
        reason = "only non-core dataset stages degraded"
    elif status == "warning" and result_count:
        # Transitional spelling for a run produced while the new table was
        # already available. Core failures above still fail closed.
        passed = True
        reason = "legacy warning with dataset receipts proving no core failure"
    elif status == "warning":
        passed = False
        reason = "legacy warning has no dataset receipts; core safety is unproven"
    else:
        passed = False
        reason = f"run status is {status}"
    return StabilityDay(
        trade_date=trade_date.isoformat(),
        run_id=run_id,
        status=status,
        dataset_results=result_count,
        passed=passed,
        reason=reason,
    )


def evaluate_stability(
    manifest: Manifest,
    trading_days: list[date],
    *,
    required_days: int = 20,
    job_name: str = "daily:core",
    as_of: date | None = None,
    now: datetime | None = None,
) -> StabilityReport:
    """Evaluate the latest *required_days* sessions without fabricating evidence.

    A degraded day counts only when dataset receipts prove that no core stage
    failed. Legacy ``warning`` rows without those receipts fail closed. Missing
    calendar sessions or missing runs also fail the gate.
    """
    if required_days < 1:
        raise ValueError("required_days must be positive")
    cutoff = as_of or date.today()
    eligible = sorted({item for item in trading_days if item <= cutoff})
    window = eligible[-required_days:]
    runs = _latest_runs_by_date(manifest, job_name)
    days = tuple(_evaluate_run(manifest, item, runs.get(item)) for item in window)

    consecutive = 0
    for item in reversed(days):
        if not item.passed:
            break
        consecutive += 1
    passed = len(days) == required_days and consecutive == required_days
    generated = now or datetime.now(timezone.utc)
    return StabilityReport(
        generated_at=generated.astimezone(timezone.utc).isoformat(),
        job_name=job_name,
        required_days=required_days,
        calendar_days_available=len(eligible),
        consecutive_passed=consecutive,
        passed=passed,
        days=days,
    )


def store_stability_report(meta_root: Path, report: StabilityReport) -> tuple[Path, Path]:
    """Persist an immutable acceptance receipt plus a latest pointer."""
    root = Path(meta_root) / "stability"
    generated = datetime.fromisoformat(report.generated_at)
    stamp = generated.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    historical = root / "history" / f"{stamp}.json"
    latest = root / "latest.json"
    payload = report.to_dict()
    write_json_atomic(historical, payload, indent=2, ensure_ascii=False)
    write_json_atomic(latest, payload, indent=2, ensure_ascii=False)
    return latest, historical


__all__ = [
    "StabilityDay",
    "StabilityReport",
    "evaluate_stability",
    "store_stability_report",
]
