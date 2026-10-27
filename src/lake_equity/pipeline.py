"""One-click data-to-result pipeline (S0-S7).

This module is the single orchestration entry behind the dashboard button, the
``pipeline`` API kind and the CLI command.  It never fabricates success: the
data update may return degraded, coverage is judged from published state, and
the process exit code is derived from the final verdict.

Layout
------
S0 preflight      configuration, trading calendar, disk headroom, active release
S1 data update    ``python -m cnequity run daily --config ... --trade-date T``
S2 coverage       one ``lake_coverage_v1`` conclusion for every registered dataset
S3 materialize    freeze the research input only when the A-tier identity changed
S4 default method resolve the active release and decide whether a refit is due
S5 selection      frozen-model scan over the whole market
S6 backtest       default protocol over the evaluable window
S7 summary        ``var/pipeline/<pipeline_id>/{pipeline.json,coverage.json,report.json}``

Every artifact is written atomically (temporary file + ``os.replace``).  The
pipeline never issues an exchange or paper order; results stay research-only.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lake_equity.jobs import JobCancelled, JobTerminationError
from lake_equity.update_progress import UpdateProgress

__all__ = ["PipelineService", "run_data_update"]

SHANGHAI = ZoneInfo("Asia/Shanghai")

SCHEMA_REPORT = "lake_pipeline_v1"
SCHEMA_STATE = "lake_pipeline_state_v1"
SCHEMA_COVERAGE = "lake_coverage_v1"
SCHEMA_PREVIEW = "lake_pipeline_preview_v1"

PIPELINE_ID_PATTERN = re.compile(r"pipe-[0-9a-f]{16}")
PACKS = ("core", "extended", "full")
REFIT_MODES = ("auto", "never", "now")

#: Fallback A-tier contract when the release module exposes no ``data_contract``
#: helper.  This is the set the current default method needs to score one day.
REQUIRED_DATASETS = ["daily_bars", "trading_calendar"]

#: Disk headroom rule from the plan: research input size x safety + fixed reserve.
RESEARCH_INPUT_BYTES = int(4.57 * 1024 ** 3)
DISK_REQUIRED_BYTES = int(RESEARCH_INPUT_BYTES * 1.2) + 20 * 1024 ** 3

#: A session closes at 15:00 in Shanghai; the pipeline only targets a closed day.
SESSION_CLOSE = day_time(15, 30)

REFIT_MIN_SESSIONS = 20
REFIT_MIN_DAYS = 30

STAGE_TITLES = {
    "S0": "预检",
    "S1": "数据更新",
    "S2": "覆盖判定",
    "S3": "冻结研究输入",
    "S4": "默认方法维护",
    "S5": "默认方法选股",
    "S6": "默认方法回测",
    "S7": "汇总报告",
}

_OUTCOMES_DEGRADED = {"warning", "degraded", "partial", "pending", "unknown", "failed",
                      "blocked", "incomplete"}
_COVERAGE_COMPLETE = "complete"
_COVERAGE_PARTIAL = "partial"
_COVERAGE_UNKNOWN = "unknown"
_BUCKETS = ("complete", "partial", "unknown", "failed")


class _PipelineBlocked(Exception):
    """Internal control-flow marker: a stage verdict forbids later stages."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Small filesystem / clock helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _jsonable(value):
    """JSON-safe copy: non-finite floats become null instead of invalid JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _is_preview(record: dict) -> bool:
    return (record.get("kind") == "preview"
            or bool((record.get("params") or {}).get("dry_run"))
            or bool((record.get("config") or {}).get("dry_run")))


def _write_json_atomic(path: Path, payload) -> None:
    """Write JSON so a reader never observes a half-written document."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, default=str,
                         indent=1, allow_nan=False)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.building")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _disk_free_bytes(root: Path) -> int:
    try:
        usage = shutil.disk_usage(str(root))
    except OSError:
        return 0
    return int(usage.free)


def _iso_date(value, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)")
    return value


def _notify(progress, **payload) -> None:
    if progress is None:
        return
    try:
        progress(payload)
    except TypeError:
        progress(**payload)


def _stopping(check_cancel) -> bool:
    return bool(check_cancel and check_cancel())


def _guard(check_cancel) -> None:
    if _stopping(check_cancel):
        raise JobCancelled("一键流水线已取消")


@contextmanager
def _operation_window(lock, check_cancel):
    """Hold the shared data-write lock for one stage, release it in ``finally``."""
    if lock is None:
        yield
        return
    while True:
        try:
            acquired = lock.acquire(timeout=0.2)
        except TypeError:  # A lock without timeout support (plain acquire).
            acquired = lock.acquire()
        except Exception as error:  # filelock raises Timeout instead of returning False.
            if type(error).__name__ not in {"Timeout", "AlreadyLocked"}:
                raise
            acquired = False
        if acquired:
            break
        if _stopping(check_cancel):
            raise JobCancelled("等待数据写入锁时已取消")
    try:
        yield
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Configuration / registry / calendar
# ---------------------------------------------------------------------------


def _config_path(root: Path) -> Path:
    return Path(root) / "configs" / "cnequity.toml"


def _config_steps(root: Path) -> dict:
    """Read the scheduled dataset steps straight from ``configs/cnequity.toml``."""
    result = {"exists": False, "error": None, "daily": set(), "events": set()}
    path = _config_path(root)
    result["exists"] = path.is_file()
    if not result["exists"]:
        return result
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
        return result
    job = data.get("job") or {}
    daily = job.get("daily") or {}
    for group in (daily.get("groups") or {}).values():
        if isinstance(group, dict):
            result["daily"].update(str(step) for step in group.get("steps") or [])
    for wave in daily.get("waves") or []:
        if isinstance(wave, dict):
            result["daily"].update(str(step) for step in wave.get("steps") or [])
    events = job.get("events") or {}
    for group in (events.get("groups") or {}).values():
        if isinstance(group, dict):
            result["events"].update(str(step) for step in group.get("steps") or [])
    return result


def _dataset_registry() -> dict:
    """Registered dataset specs, or ``{}`` when the upstream package is absent."""
    try:
        from cnequity.domain.datasets import DATASETS

        return dict(DATASETS)
    except Exception:
        return {}


def _disabled_datasets(root: Path, registry: dict, required: list[str]) -> set[str]:
    """Datasets the current configuration turns off (A tier is never disabled)."""
    names = [name for name in registry if name not in required]
    if not names:
        return set()
    try:
        from cnequity.config import load_config
        from cnequity.domain.datasets import is_dataset_enabled

        config = load_config(str(_config_path(root)))
    except Exception:
        return set()
    disabled: set[str] = set()
    for name in names:
        try:
            if not is_dataset_enabled(name, config):
                disabled.add(name)
        except Exception:
            continue
    return disabled


def _calendar_sessions(root: Path, lake=None) -> tuple[list[str], str, str | None]:
    """Return ``(sessions, source, error)`` for the verified trading calendar.

    The verified calendar is the authority.  When it is missing (fresh machine,
    trimmed artifacts) the published ``trading_calendar`` coverage is used so
    the pipeline can still resolve a target date, and the degradation is
    reported instead of blocking every stage.
    """
    calendar_error = None
    try:
        from my_strategy.services.czsc_trend_support import _calendar

        dates, identity = _calendar()
        sessions = sorted({str(item) for item in dates})
        if sessions:
            return sessions, f"verified_calendar:{identity}", None
        calendar_error = "Verified trading calendar is empty"
    except (ImportError, OSError, ValueError, KeyError, FileNotFoundError) as error:
        calendar_error = f"{type(error).__name__}: {error}"
    sessions = _published_sessions(root, lake)
    if sessions:
        return sessions, "published_trading_calendar", calendar_error
    return [], "unavailable", calendar_error


def _coverage_catalog(lake) -> dict:
    """Read coverage without the writer locks created by the upstream catalog."""
    config = getattr(lake, "config", None)
    if config is None:
        return lake.catalog()
    from cnequity.domain.datasets import DATASETS
    from cnequity.query.reader import _catalog_coverage_bounds, dataset_has_parquet

    rows = []
    for name, spec in sorted(DATASETS.items()):
        state = lake.state(name)
        stored = _read_json(config.meta_root / "state" / f"{name}.json") or {}
        directory = (config.derived_root if spec.layer == "derived" else config.curated_root) / name
        has_data = dataset_has_parquet(directory, dataset=name, meta_root=config.meta_root)
        first, last = _catalog_coverage_bounds(config, name) if has_data and spec.partition_col else (None, None)
        rows.append({"name": name, "has_data": has_data, "revision_id": state.get("revision_id"),
                     "coverage_start": first.isoformat() if first else None,
                     "coverage_end": last.isoformat() if last else None,
                     "watermark": stored.get("last_success_trade_date") if spec.watermark else None,
                     "snapshot_date": stored.get("last_snapshot_date")
                     if not spec.watermark and spec.fetch_semantics == "snapshot" else None})
    return {"datasets": rows}


def _published_sessions(root: Path, lake=None) -> list[str]:
    """Fallback sessions derived from the published ``trading_calendar`` dataset."""
    start = end = None
    if lake is not None:
        try:
            for row in (_coverage_catalog(lake) or {}).get("datasets") or []:
                if row.get("name") == "trading_calendar" or row.get("dataset") == "trading_calendar":
                    start = row.get("coverage_start")
                    end = row.get("coverage_end") or row.get("watermark")
                    break
        except Exception:
            start = end = None
    state = _read_json(Path(root) / "var/lake/meta/state/trading_calendar.json") or {}
    end = end or state.get("observed_max") or state.get("last_success_trade_date")
    start = start or state.get("complete_through")
    try:
        first = date.fromisoformat(start) if start else None
        last = date.fromisoformat(end) if end else None
    except ValueError:
        return []
    if first is None or last is None or first > last:
        return []
    sessions, cursor = [], first
    while cursor <= last:
        if cursor.weekday() < 5:
            sessions.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return sessions


def _default_target_date(root: Path, lake=None, now: datetime | None = None):
    """Resolve the last closed session; returns ``(target, sessions, source, error)``."""
    sessions, source, error = _calendar_sessions(root, lake)
    moment = now or datetime.now(SHANGHAI)
    today = moment.date().isoformat()
    clock = moment.timetz().replace(tzinfo=None)
    closed = [day for day in sessions
              if day < today or (day == today and clock >= SESSION_CLOSE)]
    target = closed[-1] if closed else None
    if target is None and sessions:
        earlier = [day for day in sessions if day < today]
        target = earlier[-1] if earlier else None
    return target, sessions, source, error


def _resolve_release(root: Path) -> tuple[dict | None, str | None]:
    """Load the active strategy release, reporting rather than hiding failures."""
    try:
        from lake_equity.learning.releases import ReleaseRegistry

        release = ReleaseRegistry(root).active()
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"
    if release is None:
        return None, "No active research release; publish and activate a default method first"
    return release, None


def _release_summary(release: dict | None, error: str | None) -> dict:
    if release is None:
        return {"available": False, "error": error}
    universe = release.get("universe") if isinstance(release.get("universe"), dict) else {}
    contract = release.get("data_contract")
    if not contract:
        try:
            from lake_equity.learning import releases as releases_module

            contract = releases_module.data_contract(release)
        except Exception:
            contract = None
    return {
        "available": True,
        "release_id": release.get("release_id"),
        "name": release.get("name"),
        "model_id": release.get("model_id"),
        "series_id": release.get("series_id"),
        "study_id": release.get("study_id"),
        "information_cutoff": release.get("information_cutoff"),
        "created_at": release.get("created_at"),
        "read_ref": release.get("read_ref"),
        "protocol": release.get("protocol"),
        "risk": release.get("risk"),
        "universe_policy": universe.get("policy"),
        "qualification": release.get("qualification"),
        "data_contract": contract,
    }


def _contract_names(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if isinstance(value, dict):
        for key in ("required_datasets", "datasets", "required", "names"):
            if key in value:
                return _contract_names(value[key])
    return []


def _required_datasets(release: dict | None) -> tuple[list[str], str]:
    """A-tier datasets: the release contract if present, else the fixed fallback."""
    if release:
        try:
            from lake_equity.learning import releases as releases_module

            contract = getattr(releases_module, "data_contract", None)
            if callable(contract):
                names = _contract_names(contract(release))
                if names:
                    return list(dict.fromkeys(names)), "release_data_contract"
        except Exception:
            pass
    return list(REQUIRED_DATASETS), "fallback_default_contract"


# ---------------------------------------------------------------------------
# Manifest / state evidence
# ---------------------------------------------------------------------------


def _manifest_results(root: Path) -> dict[str, dict]:
    """Latest recorded result per dataset from ``meta/manifest.db`` (read-only)."""
    path = Path(root) / "var/lake/meta/manifest.db"
    if not path.is_file():
        return {}
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    result: dict[str, dict] = {}
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT r.run_id, r.started_at, r.finished_at, r.status AS run_status, "
            "d.dataset, d.stage, d.status, d.execution_status, d.coverage_status, "
            "d.publication_status, d.reason_code, d.rows_written "
            "FROM dataset_results d JOIN ingestion_runs r ON d.run_id = r.run_id "
            "ORDER BY r.started_at DESC LIMIT 20000").fetchall()
        for row in rows:
            record = dict(row)
            if record["dataset"] in result:
                continue
            result[record["dataset"]] = record
    except sqlite3.Error:
        return result
    finally:
        connection.close()
    return result


def _state_file(root: Path, name: str) -> dict:
    payload = _read_json(Path(root) / "var/lake/meta/state" / f"{name}.json")
    return payload if isinstance(payload, dict) else {}


def _merge_state(row: dict, lake_state: dict, state_file: dict) -> dict:
    merged: dict = {}
    published = row.get("published_state")
    for source in (published, lake_state, state_file):
        if isinstance(source, dict):
            merged.update({key: value for key, value in source.items() if value is not None})
    return merged


def _normalize_coverage(value) -> str:
    if value == "complete":
        return _COVERAGE_COMPLETE
    if value in {"partial", "incomplete", "warning", "degraded"}:
        return _COVERAGE_PARTIAL
    return _COVERAGE_UNKNOWN


def _missing_keys(state: dict) -> int:
    outstanding = state.get("outstanding_keys")
    if isinstance(outstanding, list):
        return len(outstanding)
    if isinstance(outstanding, int) and not isinstance(outstanding, bool):
        return outstanding
    missing = state.get("missing_ranges")
    if isinstance(missing, list):
        return len(missing)
    if isinstance(missing, int) and not isinstance(missing, bool):
        return missing
    return 0


def _state_reason(state: dict, manifest_row: dict) -> str | None:
    outstanding = state.get("outstanding_keys")
    if isinstance(outstanding, list) and outstanding and isinstance(outstanding[0], dict):
        reason = outstanding[0].get("reason") or outstanding[0].get("last_reason")
        if reason:
            return str(reason)
    return manifest_row.get("reason_code") or state.get("reason_code")


def _last_attempt(state: dict, manifest_row: dict):
    return (state.get("last_attempt") or state.get("last_success_trade_date")
            or manifest_row.get("finished_at") or manifest_row.get("started_at"))


# ---------------------------------------------------------------------------
# S2: the single coverage conclusion
# ---------------------------------------------------------------------------


def _classify(name: str, required: set[str], scheduled: set[str], disabled: set[str]) -> str:
    if name in required:
        return "A"
    if name in disabled:
        return "D"
    if name in scheduled:
        return "B"
    return "C"


def _bucket(entry: dict, target_date: str | None) -> str:
    if entry["execution_status"] == "failed" and not entry["revision_id"]:
        return "failed"
    proof = entry.get("proof_date")
    explicit = entry["coverage_status"]
    if explicit == _COVERAGE_COMPLETE:
        if not target_date or (proof and str(proof) >= str(target_date)):
            return "complete"
        return "partial"
    if explicit == _COVERAGE_PARTIAL or entry["missing_keys"] > 0:
        return "partial"
    if entry["revision_id"] and proof:
        return "partial"  # published but behind the target date
    return "unknown"


def _proof_date(state: dict, row: dict) -> str | None:
    candidates = [state.get("complete_through"), state.get("last_success_trade_date"),
                  state.get("observed_max"), state.get("observed_through"),
                  row.get("watermark"), row.get("snapshot_date"),
                  state.get("revision_at"), state.get("updated_at")]
    dates = [str(value)[:10] for value in candidates if isinstance(value, str) and len(value) >= 10]
    return max(dates) if dates else None


def _dataset_entry(root: Path, name: str, row: dict, lake_state: dict,
                   manifest_row: dict, target_date: str | None, tier: str) -> dict:
    state = _merge_state(row, lake_state, _state_file(root, name))
    revision_id = state.get("revision_id") or row.get("revision_id")
    watermark = (row.get("watermark") or row.get("snapshot_date") or state.get("observed_max")
                 or state.get("watermark"))
    complete_through = state.get("complete_through") or row.get("coverage_end")
    explicit = _normalize_coverage(state.get("coverage_status"))
    proof = _proof_date(state, row)
    coverage_status = explicit
    coverage_source = "state" if explicit != _COVERAGE_UNKNOWN else "absent"
    if (explicit == _COVERAGE_UNKNOWN and revision_id and proof
            and (not target_date or proof >= str(target_date))):
        # No explicit verdict in the state: a published revision plus a date
        # proof at/after the target is the strongest evidence the lake offers.
        coverage_status = _COVERAGE_COMPLETE
        coverage_source = "inferred"
    execution_status = manifest_row.get("execution_status") or (
        "completed" if revision_id else "failed")
    entry = {
        "name": name,
        "tier": tier,
        "execution_status": execution_status,
        "coverage_status": coverage_status,
        "coverage_source": coverage_source,
        "publication_status": manifest_row.get("publication_status") or (
            "published" if revision_id else "none"),
        "revision_id": revision_id,
        "watermark": watermark,
        "complete_through": complete_through,
        "proof_date": proof,
        "missing_keys": _missing_keys(state),
        "reason_code": _state_reason(state, manifest_row),
        "last_attempt": _last_attempt(state, manifest_row),
        "has_data": bool(row.get("has_data", revision_id is not None)),
    }
    entry["bucket"] = _bucket(entry, target_date)
    return entry


def _build_coverage(root: Path, lake, target_date: str | None, release: dict | None) -> dict:
    rows: dict[str, dict] = {}
    catalog_error = None
    try:
        for row in (_coverage_catalog(lake) or {}).get("datasets") or []:
            name = row.get("name") or row.get("dataset")
            if name:
                rows[str(name)] = dict(row)
    except Exception as error:  # A broken catalog must not read as "no data".
        catalog_error = f"{type(error).__name__}: {error}"

    registry = _dataset_registry()
    config = _config_steps(root)
    required, required_source = _required_datasets(release)
    required_set = set(required)
    disabled = _disabled_datasets(root, registry, required)
    scheduled = (config["daily"] | config["events"]) & (set(registry) | set(rows))
    manifest = _manifest_results(root)

    registered = sorted(set(registry) | set(rows) | required_set)
    datasets: list[dict] = []
    counts = {"registered": len(registered), "not_enabled": 0, "not_scheduled": 0,
              "attempted": 0, "complete": 0, "partial": 0, "unknown": 0, "failed": 0}
    attempted: list[dict] = []
    for name in registered:
        tier = _classify(name, required_set, scheduled, disabled)
        lake_state: dict = {}
        try:
            value = lake.state(name)
            lake_state = dict(value) if isinstance(value, dict) else {}
        except Exception:
            lake_state = {}
        entry = _dataset_entry(root, name, rows.get(name, {}), lake_state,
                               manifest.get(name, {}), target_date, tier)
        if tier == "D":
            # Every registered dataset gets a status; D never counts as attempted.
            entry["bucket"] = "not_enabled"
            counts["not_enabled"] += 1
            datasets.append(entry)
            continue
        if tier == "C":
            entry["bucket"] = "not_scheduled"
            counts["not_scheduled"] += 1
            datasets.append(entry)
            continue
        datasets.append(entry)
        attempted.append(entry)

    for entry in attempted:
        counts[entry["bucket"]] += 1
    counts["attempted"] = len(attempted)
    if counts["attempted"] != sum(counts[key] for key in _BUCKETS):  # defensive invariant
        raise ValueError("Coverage bucket counts are inconsistent")

    a_entries = [entry for entry in attempted if entry["tier"] == "A"]
    blocking: list[dict] = []
    for entry in a_entries:
        if entry["bucket"] != "complete":
            blocking.append({"dataset": entry["name"],
                             "reason": f"required_coverage_{entry['bucket']}"})
    a_complete = bool(a_entries) and not blocking
    if not a_entries:
        blocking.append({"dataset": "*", "reason": "required_datasets_missing"})
        a_complete = False
    if release is None:
        blocking.append({"dataset": "*", "reason": "no_active_release"})
    can_scan = bool(a_complete)
    optional_gaps = [
        {"dataset": entry["name"], "tier": entry["tier"], "bucket": entry["bucket"]}
        for entry in attempted
        if entry["tier"] in {"B", "C"} and entry["bucket"] != "complete"
    ]
    # Only the A tier (the active release's required data contract) can block or
    # degrade the pipeline. Futures, options, margin and other optional datasets
    # are reported as gaps but do not turn an otherwise complete equity run into
    # a partial one.
    verdict = "blocked" if blocking else "complete"

    return {
        "schema": SCHEMA_COVERAGE,
        "pipeline_id": None,
        "target_date": target_date,
        "calendar_session": None,
        "verdict": verdict,
        "required_datasets": required,
        "required_source": required_source,
        "can_scan": can_scan,
        "can_backtest": can_scan and release is not None,
        "datasets": datasets,
        "counts": counts,
        "blocking": blocking,
        "optional_gaps": optional_gaps,
        "catalog_error": catalog_error,
        "config": {"path": str(_config_path(root)), "exists": config["exists"],
                   "error": config["error"],
                   "scheduled_steps": len(config["daily"] | config["events"])},
        "generated_at": _now(),
    }


def _coverage_summary(coverage: dict) -> dict:
    return {
        "verdict": coverage.get("verdict"),
        "can_scan": coverage.get("can_scan"),
        "can_backtest": coverage.get("can_backtest"),
        "counts": coverage.get("counts"),
        "blocking": coverage.get("blocking"),
        "optional_gaps": coverage.get("optional_gaps"),
        "required_datasets": coverage.get("required_datasets"),
    }


# ---------------------------------------------------------------------------
# S1: the data update process
# ---------------------------------------------------------------------------


def _parse_outcomes(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    outcomes: list[dict] = []

    def inspect(value):
        if isinstance(value, dict):
            if ("execution_status" in value or "coverage_status" in value
                    or ("run_id" in value and "status" in value)):
                outcomes.append(value)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    offset = 0
    while offset < len(text):
        offset = text.find("{", offset)
        if offset < 0:
            break
        try:
            value, length = decoder.raw_decode(text[offset:])
            inspect(value)
            offset += length
        except json.JSONDecodeError:
            offset += 1
    return outcomes


def _outcome_degraded(outcomes: list[dict]) -> bool:
    if not outcomes:
        return True
    for value in outcomes:
        for field in ("status", "execution_status", "coverage_status", "publication_status"):
            if str(value.get(field, "")).lower() in _OUTCOMES_DEGRADED:
                return True
    return False


def _terminate(process) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # The venv executable is a launcher; terminating only it leaves the
        # real interpreter (and collection workers) running on Windows.
        try:
            stopped = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if stopped.returncode:
                raise JobTerminationError(f"无法确认采集进程树 {process.pid} 已停止，请检查任务进程。")
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise JobTerminationError(f"停止采集进程树 {process.pid} 失败，请检查任务进程。") from error
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise JobTerminationError(f"停止采集进程 {process.pid} 失败，请检查任务进程。") from error


def _read_log_segment(path: Path, offset: int) -> str:
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return ""


def _execute_update(command: list[str], cwd: Path, log_path: Path, progress, cancelled):
    """Run the upstream CLI once, appending its stdout to the shared daily log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with log_path.open("a", encoding="utf-8") as output:
        start_offset = output.tell()
        monitor = UpdateProgress(cwd, log_path, start_offset)
        process = subprocess.Popen(command, cwd=str(cwd), stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=creation)
        try:
            while process.poll() is None:
                if cancelled is not None and cancelled():
                    raise JobCancelled("数据更新已取消，已发布批次保留")
                _notify(progress, phase="cne_data_update", pid=process.pid,
                        log=log_path.name, **monitor.sample())
                time.sleep(1)
        except BaseException:
            _terminate(process)
            raise
    return process.returncode, _read_log_segment(log_path, start_offset)


def run_data_update(settings, lake, params, progress=None, cancelled=None,
                    operation_lock=None) -> dict:
    """S1: run one CNEquity daily update and describe the result.

    Never raises for a non-zero upstream exit code; the caller decides whether a
    failed update still leaves enough data for later stages.  ``settings`` may be
    a :class:`lake_equity.settings.Settings` or a plain project root path.
    """
    if isinstance(settings, (str, Path)):
        root = Path(settings).resolve()
    else:
        root = Path(getattr(settings, "root", settings)).resolve()
    values = dict(params or {})
    target = values.get("end")
    if target is not None:
        target = _iso_date(target, "end")
    packs = values.get("packs", "full")
    if packs not in PACKS:
        raise ValueError("packs must be one of " + ", ".join(PACKS))
    config_path = _config_path(root)
    log_path = root / "var/logs/cne-daily.log"
    command = [sys.executable, "-m", "cnequity", "run", "daily", "--config", str(config_path)]
    if target:
        command += ["--trade-date", target]
    if packs == "core":
        command += ["--core-only"]
    result = {
        "status": "skipped",
        "reason": None,
        "exit_code": None,
        "outcomes": [],
        "outcome_assessed": False,
        "degraded": False,
        "log_tail": "",
        "log": str(log_path),
        "command": command,
        "packs": packs,
        "target_date": target,
    }
    if values.get("dry_run"):
        result["reason"] = "dry_run"
        return result
    if values.get("skip_update"):
        result["reason"] = "skip_update"
        return result
    if not config_path.is_file():
        raise ValueError("缺少 configs/cnequity.toml；请先完成本地导入与配置")
    _notify(progress, phase="cne_data_update_start", command=command, log=log_path.name,
            message="等待数据写入锁，随后开始采集")
    with _operation_window(operation_lock, cancelled):
        returncode, text = _execute_update(command, root, log_path, progress, cancelled)
    outcomes = _parse_outcomes(text)
    degraded = _outcome_degraded(outcomes)
    result.update(exit_code=returncode, outcomes=outcomes, outcome_assessed=bool(outcomes),
                  degraded=degraded, log_tail=text[-5000:])
    if returncode:
        result.update(status="failed", reason=f"upstream_exit_{returncode}")
    elif degraded:
        result.update(status="partial", reason="outcomes_degraded")
    else:
        result.update(status="completed", reason=None)
    return result


# ---------------------------------------------------------------------------
# S3/S4/S5/S6 helpers
# ---------------------------------------------------------------------------


def _materialize(root: Path, lake, coverage: dict, params: dict, progress, check_cancel,
                 operation_lock) -> dict:
    detail = {"status": "skipped", "reason": None, "skipped": True,
              "manifest_id": None, "read_ref": None, "read_ref_source": None,
              "revision_map_before": None, "desired_revision_map": None}
    if params.get("dry_run"):
        detail["reason"] = "dry_run"
        return detail
    if not coverage.get("can_scan"):
        detail["reason"] = "coverage_blocked"
        return detail
    desired = {entry["name"]: entry["revision_id"] for entry in coverage["datasets"]
               if entry["tier"] == "A"}
    detail["desired_revision_map"] = desired
    manifest_path = root / "var/research_inputs/current/khquant_raw.manifest.json"
    existing = _read_json(manifest_path) or {}
    current_map = existing.get("revision_map") if isinstance(existing.get("revision_map"), dict) else {}
    detail["revision_map_before"] = current_map
    frozen = bool(desired) and all(current_map.get(name) and current_map.get(name) == identity
                                   for name, identity in desired.items())
    if frozen:
        detail.update(skipped=True, reason="already_frozen",
                      manifest_id=existing.get("manifest_id"))
        return detail
    _guard(check_cancel)
    with _operation_window(operation_lock, check_cancel):
        _notify(progress, phase="materialize", desired_revision_map=desired)
        materialized = lake.materialize_legacy(progress=progress, check_cancel=check_cancel)
    refreshed = _read_json(manifest_path) or {}
    manifest_id = (materialized or {}).get("manifest_id") or refreshed.get("manifest_id")
    updated_map = refreshed.get("revision_map") if isinstance(refreshed.get("revision_map"), dict) else {}
    read_ref = refreshed.get("read_ref") or refreshed.get("manifest_id")
    detail.update(status="completed", skipped=False, reason="materialized",
                  manifest_id=manifest_id, read_ref=read_ref,
                  read_ref_source="input_manifest" if refreshed.get("read_ref") else "manifest_id_fallback",
                  revision_map_after=updated_map,
                  snapshot_db=(materialized or {}).get("snapshot_db"))
    return detail


def _refit_due(root: Path, release: dict, target: str | None, sessions: list[str],
               now: datetime) -> tuple[bool, dict]:
    """Prefer ``learning.defaults.refit_due``; keep a local rule when unavailable."""
    if target:
        try:
            from lake_equity.learning import defaults as defaults_module

            function = getattr(defaults_module, "refit_due", None)
            if callable(function):
                value = function(root, release, target)
                if isinstance(value, dict) and "due" in value:
                    return bool(value.get("due")), {**value,
                                                   "source": "learning.defaults.refit_due"}
        except Exception:
            pass  # Fall through to the local rule; the report still records the decision.
    cutoff = release.get("information_cutoff")
    sessions_after = [day for day in sessions
                      if cutoff and day > cutoff and (not target or day <= target)]
    created = None
    if isinstance(release.get("created_at"), str):
        try:
            created = datetime.fromisoformat(release["created_at"].replace("Z", "+00:00"))
        except ValueError:
            created = None
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=SHANGHAI)
    days = (now - created).total_seconds() / 86400 if created is not None else None
    due = bool(len(sessions_after) >= REFIT_MIN_SESSIONS and days is not None
               and days >= REFIT_MIN_DAYS)
    return due, {
        "information_cutoff": cutoff,
        "sessions_since_cutoff": len(sessions_after),
        "sessions_required": REFIT_MIN_SESSIONS,
        "days_since_created": round(days, 1) if days is not None else None,
        "days_required": REFIT_MIN_DAYS,
        "source": "local_refit_rule",
    }


def _call_refit(root: Path, release: dict, target: str | None, progress, check_cancel):
    """Call ``learning.defaults.refit_and_candidate`` when it is available."""
    try:
        from lake_equity.learning import defaults as defaults_module
    except ImportError:
        return None, "learning.defaults is not available"
    function = getattr(defaults_module, "refit_and_candidate", None)
    if not callable(function):
        return None, "learning.defaults.refit_and_candidate is not available"
    import inspect

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs = {}
    for name, value in (("root", root), ("release", release), ("target_date", target),
                        ("end", target), ("progress", progress),
                        ("check_cancel", check_cancel)):
        if name in parameters:
            kwargs[name] = value
    try:
        return function(**kwargs), None
    except TypeError:
        return function(root, release, target), None


def _resolve_default(root: Path, release: dict | None, release_error: str | None, params: dict,
                     target: str | None, sessions: list[str], now: datetime,
                     progress=None, check_cancel=None) -> dict:
    mode = params.get("refit", "auto")
    detail = {"status": "completed", "mode": mode, "action": "reuse", "release_id": None,
              "refit_due": False, "refit_due_detail": None, "candidate_model_id": None,
              "candidate_release_id": None, "reason": None, "error": None}
    if release is None:
        detail.update(status="blocked", action="unavailable", reason="no_active_release",
                      error=release_error)
        return detail
    detail["release_id"] = release.get("release_id")
    due, due_detail = _refit_due(root, release, target, sessions, now)
    detail["refit_due"] = due
    detail["refit_due_detail"] = due_detail
    if mode == "never":
        detail["action"] = "reuse"
        return detail
    if mode == "auto":
        detail["action"] = "reuse_refit_due" if due else "reuse"
        return detail
    _guard(check_cancel)
    try:
        value, error = _call_refit(root, release, target, progress, check_cancel)
    except JobCancelled:
        raise
    except Exception as failure:
        detail.update(status="failed", action="refit_failed",
                      error=f"{type(failure).__name__}: {failure}")
        return detail
    if value is None:
        detail.update(status="skipped", action="refit_unavailable",
                      reason=error or "learning.defaults.refit_and_candidate is not available")
        return detail
    candidate = None
    if isinstance(value, dict):
        candidate = value.get("model_id") or value.get("candidate_model_id")
    detail.update(action="refit", candidate_model_id=candidate,
                  candidate_release_id=(value.get("release_id") if isinstance(value, dict) else None),
                  result=value if isinstance(value, dict) else {"value": str(value)})
    if isinstance(value, dict) and value.get("error"):
        detail.update(status="failed", action="refit_failed", error=str(value["error"]))
    return detail


def _workflow(root: Path):
    from lake_equity.learning.workflow import LearningWorkflow

    return LearningWorkflow(root)


def _scan_summary(report: dict, top_k: int) -> dict:
    population = report.get("population") if isinstance(report.get("population"), dict) else {}
    final_ranking = bool(report.get("final_ranking"))
    scored = int(population.get("scored") or 0)
    top = report.get("top") if isinstance(report.get("top"), list) else []
    publishable = bool(final_ranking and scored > 0)
    return {
        "run_id": report.get("run_id"),
        "status": report.get("status"),
        "final_ranking": final_ranking,
        "as_of": report.get("as_of"),
        "requested_cutoff": report.get("requested_cutoff"),
        "release_id": report.get("release_id"),
        "population": {key: population.get(key) for key in
                       ("total", "scored", "missing", "failed", "unprocessed", "excluded")},
        "scored": scored,
        "candidates_written": publishable,
        "top": [{"symbol": row.get("symbol"), "score": row.get("score"), "rank": row.get("rank")}
                for row in top[:top_k]] if publishable else [],
        "read_ref": report.get("read_ref"),
        "error": None,
        "reason": None,
    }


def _run_scan(root: Path, coverage: dict, params: dict, target: str | None, progress,
              check_cancel) -> dict:
    detail = {"status": "skipped", "reason": None, "run_id": None, "final_ranking": None,
              "scored": None, "candidates_written": False, "top": [], "population": None,
              "as_of": None, "read_ref": None, "error": None}
    if params.get("dry_run"):
        detail["reason"] = "dry_run"
        return detail
    if not coverage.get("can_scan"):
        detail["reason"] = "coverage_blocked"
        return detail
    if not params.get("scan", True):
        detail["reason"] = "disabled_by_params"
        return detail
    _guard(check_cancel)
    try:
        report = _workflow(root).scan({"scope": "all", "symbols": [], "end": target,
                                       "top_k": params.get("top_k", 20),
                                       "release_id": "active"},
                                      progress=progress, check_cancel=check_cancel)
    except JobCancelled:
        raise
    except Exception as error:
        detail.update(status="blocked", reason="scan_error",
                      error=f"{type(error).__name__}: {error}")
        return detail
    summary = _scan_summary(report or {}, params.get("top_k", 20))
    detail.update(summary)
    if not (summary["final_ranking"] and summary["scored"] > 0):
        detail.update(status="blocked", candidates_written=False, top=[],
                      reason="no_final_ranking" if not summary["final_ranking"]
                      else "no_scored_candidates")
    else:
        detail["status"] = "completed"
    return detail


def _next_session_after(sessions: list[str], cutoff: str | None, target: str | None):
    if not cutoff:
        return None
    later = [day for day in sessions if day > cutoff and (not target or day <= target)]
    if later:
        return later[0]
    try:
        return (date.fromisoformat(cutoff) + timedelta(days=1)).isoformat()
    except ValueError:
        return None


def _default_window(root: Path, release: dict, target: str, sessions: list[str]) -> dict:
    """Prefer ``learning.defaults.default_window``; keep the local derivation as fallback."""
    try:
        from lake_equity.learning import defaults as defaults_module

        function = getattr(defaults_module, "default_window", None)
        if callable(function):
            value = function(root, release, target)
            if (isinstance(value, dict) and value.get("mode") and value.get("start")
                    and value.get("end")):
                return {"mode": value["mode"], "start": str(value["start"]),
                        "end": str(value["end"]), "source": "learning.defaults.default_window",
                        "reason": value.get("reason"), "series_id": release.get("series_id"),
                        "series_error": None}
    except Exception:
        pass  # Recorded by the caller through the stage detail.
    window = {"mode": "frozen", "start": None, "end": target, "source": "information_cutoff",
              "series_id": release.get("series_id"), "series_error": None}
    series_id = release.get("series_id")
    if series_id:
        try:
            from lake_equity.learning.registry import ModelRegistry

            series = ModelRegistry(root).read_series(series_id)
            members = series.get("members") or []
            if members:
                start = min(str(member["start"]) for member in members)
                end = min(max(str(member["end"]) for member in members), target)
                window.update(mode="historical", start=start, end=end, source="release_series")
                return window
            window["series_error"] = "Saved series has no members"
        except Exception as error:
            window["series_error"] = f"{type(error).__name__}: {error}"
    window.update(mode="frozen", start=_next_session_after(sessions,
                                                           release.get("information_cutoff"),
                                                           target),
                  source="information_cutoff")
    return window


def _backtest_summary(report: dict) -> dict:
    portfolio = report.get("portfolio") if isinstance(report.get("portfolio"), dict) else {}
    metrics = portfolio.get("metrics") if isinstance(portfolio.get("metrics"), dict) else {}
    trades = portfolio.get("trades") if isinstance(portfolio.get("trades"), list) else []
    scalars = {key: value for key, value in metrics.items()
               if isinstance(value, (int, float, str, bool)) or value is None}
    return {
        "run_id": report.get("run_id"),
        "status": report.get("status"),
        "mode": report.get("mode"),
        "actual_start": report.get("actual_start") or portfolio.get("evaluation_start"),
        "actual_end": report.get("actual_end") or portfolio.get("evaluation_end"),
        "performance_status": "available" if metrics else "unavailable",
        "performance": scalars,
        "trades": len(trades),
        "fills": metrics.get("fills"),
        "read_ref": report.get("read_ref"),
        "release_id": report.get("release_id"),
        "error": None,
    }


def _run_backtest(root: Path, coverage: dict, params: dict, target: str | None, release: dict,
                  sessions: list[str], progress, check_cancel) -> dict:
    detail = {"status": "skipped", "reason": None, "run_id": None, "mode": None, "start": None,
              "end": None, "window_source": None, "performance_status": "unavailable",
              "performance": None, "trades": None, "read_ref": None, "error": None}
    if params.get("dry_run"):
        detail["reason"] = "dry_run"
        return detail
    if not coverage.get("can_backtest"):
        detail["reason"] = "coverage_blocked"
        return detail
    if not params.get("backtest", True):
        detail["reason"] = "disabled_by_params"
        return detail
    _guard(check_cancel)
    window = _default_window(root, release, target, sessions)
    detail.update(start=window["start"], end=window["end"], mode=window["mode"],
                  window_source=window["source"], series_error=window.get("series_error"))
    if not window["start"] or window["start"] > window["end"]:
        detail.update(status="unavailable", reason="no_evaluable_window",
                      error="No evaluable backtest window after the release information cutoff")
        return detail
    request = {"mode": window["mode"], "release_id": "active", "start": window["start"],
               "end": window["end"], "top_k": 10, "initial_cash": 1_000_000}
    try:
        report = _workflow(root).backtest(request, progress=progress, check_cancel=check_cancel)
    except JobCancelled:
        raise
    except ValueError as error:
        detail.update(status="unavailable", reason="insufficient_data",
                      error=f"{type(error).__name__}: {error}")
        return detail
    except Exception as error:
        detail.update(status="unavailable", reason="backtest_error",
                      error=f"{type(error).__name__}: {error}")
        return detail
    detail.update(_backtest_summary(report or {}))
    detail["status"] = "completed"
    return detail


# ---------------------------------------------------------------------------
# Parameters, stages and status decision
# ---------------------------------------------------------------------------


def _normalize_params(params: dict | None) -> dict:
    values = dict(params or {})
    allowed = {"end", "packs", "refit", "skip_update", "dry_run", "scan", "backtest", "top_k"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("Unknown pipeline parameters: " + ", ".join(sorted(unknown)))
    end = values.get("end")
    if end is not None:
        end = _iso_date(end, "end")
    packs = values.get("packs", "full")
    if packs not in PACKS:
        raise ValueError("packs must be one of " + ", ".join(PACKS))
    refit = values.get("refit", "auto")
    if refit not in REFIT_MODES:
        raise ValueError("refit must be one of " + ", ".join(REFIT_MODES))
    normalized = {"end": end, "packs": packs, "refit": refit}
    for key, default in (("skip_update", False), ("dry_run", False), ("scan", True),
                         ("backtest", True)):
        value = values.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        normalized[key] = value
    top_k = values.get("top_k", 20)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
        raise ValueError("top_k must be an integer in [1,100]")
    normalized["top_k"] = top_k
    return normalized


def _stage(stage_id: str, title: str) -> dict:
    return {"id": stage_id, "title": title, "status": "pending", "started_at": None,
            "finished_at": None, "detail": {}}


def _begin(stage: dict, **detail) -> dict:
    stage["started_at"] = _now()
    stage["status"] = "running"
    if detail:
        stage["detail"] = {**stage.get("detail", {}), **detail}
    return stage


def _finish(stage: dict, stage_status: str, **detail) -> dict:
    stage["finished_at"] = _now()
    stage["status"] = stage_status
    if detail:
        stage["detail"] = {**stage.get("detail", {}), **detail}
    return stage


def _decide_status(blocked: list[dict], update: dict, refit: dict, scan: dict,
                   backtest: dict, coverage: dict) -> tuple[str, int]:
    if blocked:
        return "blocked", 4
    if coverage.get("verdict") == "blocked":
        return "blocked", 4
    if refit.get("status") == "blocked":
        return "blocked", 4
    if scan.get("status") == "blocked":
        return "blocked", 4
    data_degraded: set[str] = set()
    if update.get("status") in {"failed", "partial"}:
        data_degraded.add("data_update")
    if backtest.get("status") in {"unavailable", "failed"}:
        data_degraded.add("backtest_unavailable")
    if refit.get("status") == "failed":
        data_degraded.add("refit_failed")
    if data_degraded - {"backtest_unavailable", "refit_failed"}:
        return "partial", 3
    if data_degraded:
        return "completed_degraded", 3
    return "completed", 0


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PipelineService:
    """One-click pipeline: preview is read-only, run owns S0-S7 and the artifacts."""

    def __init__(self, root, lake, operation_lock=None):
        self.root = Path(root).resolve()
        self.lake = lake
        self.operation_lock = operation_lock

    # -- public API --------------------------------------------------------

    def preview(self, params: dict) -> dict:
        values = _normalize_params(params)
        now = datetime.now(SHANGHAI)
        if values["end"]:
            target = values["end"]
            sessions, calendar_source, calendar_error = _calendar_sessions(self.root, self.lake)
        else:
            target, sessions, calendar_source, calendar_error = _default_target_date(
                self.root, self.lake, now)
        release, release_error = _resolve_release(self.root)
        coverage = _build_coverage(self.root, self.lake, target, release)
        if target is not None:
            coverage["calendar_session"] = target in sessions if sessions else None
        preflight = self._preflight(target, sessions, calendar_source, calendar_error,
                                    release, release_error)
        blockers = list(preflight["blockers"])
        blockers += [{"code": "coverage_blocked",
                      "message": f"required dataset {item['dataset']} is {item['reason']}"}
                     for item in coverage["blocking"] if item.get("dataset") != "*"]
        if not coverage.get("can_scan") and coverage.get("verdict") != "blocked":
            blockers.append({"code": "not_scannable",
                             "message": "Coverage does not allow selection"})
        return {
            "schema": SCHEMA_PREVIEW,
            "kind": "preview",
            "status": "blocked" if blockers else "ready",
            "status_label": "只读预检",
            "exit_code": 4 if blockers else 0,
            "target_date": target,
            "calendar": {"source": calendar_source, "sessions": len(sessions),
                         "error": calendar_error, "session": coverage["calendar_session"]},
            "coverage": coverage,
            "active_release": _release_summary(release, release_error),
            "plan": self._plan(values, coverage),
            "can_scan": bool(coverage.get("can_scan")),
            "can_backtest": bool(coverage.get("can_backtest")),
            "blockers": blockers,
            "config": {"root": str(self.root), "config_path": str(_config_path(self.root)),
                       "packs": values["packs"], "refit": values["refit"],
                       "dry_run": values["dry_run"], "skip_update": values["skip_update"],
                       "scan": values["scan"], "backtest": values["backtest"],
                       "top_k": values["top_k"]},
        }

    def run(self, params: dict, progress=None, check_cancel=None) -> dict:
        values = _normalize_params(params)
        pipeline_id = "pipe-" + uuid.uuid4().hex[:16]
        created_at = _now()
        now = datetime.now(SHANGHAI)
        stages = {stage_id: _stage(stage_id, title) for stage_id, title in STAGE_TITLES.items()}
        report = {
            "schema": SCHEMA_REPORT,
            "kind": "preview" if values["dry_run"] else "pipeline",
            "pipeline_id": pipeline_id,
            "status": "failed",
            "exit_code": 1,
            "target_date": None,
            "params": values,
            "config": {"root": str(self.root), "config_path": str(_config_path(self.root))},
            "stages": [],
            "coverage": None,
            "coverage_summary": None,
            "active_release": _release_summary(None, None),
            "refit": None,
            "materialize": None,
            "data_update": None,
            "scan": None,
            "backtest": None,
            "manifest_id": None,
            "read_ref": None,
            "error": None,
            "created_at": created_at,
            "finished_at": None,
        }
        if values["dry_run"]:
            report["status_label"] = "只读预检（历史记录）"
        update: dict = {"status": "skipped", "reason": "not_started", "outcomes": []}
        refit: dict = {}
        scan: dict = {}
        backtest: dict = {}
        coverage: dict = {"verdict": "unknown", "can_scan": False, "can_backtest": False,
                          "datasets": [], "blocking": [], "counts": {}}
        blockers: list[dict] = []
        cancelled = False
        failed: str | None = None
        try:
            _guard(check_cancel)
            # S0 preflight ---------------------------------------------------
            _begin(stages["S0"])
            target = values["end"]
            sessions, calendar_source, calendar_error = _calendar_sessions(self.root, self.lake)
            if target is None:
                target, sessions, calendar_source, calendar_error = _default_target_date(
                    self.root, self.lake, now)
            release, release_error = _resolve_release(self.root)
            preflight = self._preflight(target, sessions, calendar_source, calendar_error,
                                        release, release_error)
            report["target_date"] = target
            report["active_release"] = _release_summary(release, release_error)
            blockers.extend(preflight["blockers"])
            _finish(stages["S0"], "blocked" if blockers else "completed", **preflight)
            if blockers:
                # Still report the coverage conclusion; it explains the preflight block.
                coverage = _build_coverage(self.root, self.lake, target, release)
                coverage["pipeline_id"] = pipeline_id
                report["coverage"] = coverage
                report["coverage_summary"] = _coverage_summary(coverage)
                for stage_id in ("S1", "S2", "S3", "S4", "S5", "S6"):
                    _finish(stages[stage_id], "skipped", reason="preflight_blocked")
                raise _PipelineBlocked("preflight")
            # S1 data update -------------------------------------------------
            _begin(stages["S1"])
            update = run_data_update(self.root, self.lake,
                                     {"end": target, "packs": values["packs"],
                                      "dry_run": values["dry_run"],
                                      "skip_update": values["skip_update"]},
                                     progress=self._progress(progress, "S1"),
                                     cancelled=check_cancel,
                                     operation_lock=self.operation_lock)
            report["data_update"] = {key: update.get(key) for key in
                                     ("status", "reason", "exit_code", "outcome_assessed",
                                      "degraded", "log")}
            _finish(stages["S1"], update["status"], **report["data_update"])
            _guard(check_cancel)
            # S2 coverage ----------------------------------------------------
            _begin(stages["S2"])
            coverage = _build_coverage(self.root, self.lake, target, release)
            coverage["pipeline_id"] = pipeline_id
            coverage["calendar_session"] = target in sessions if sessions else None
            report["coverage"] = coverage
            report["coverage_summary"] = _coverage_summary(coverage)
            _finish(stages["S2"], "blocked" if coverage["verdict"] == "blocked" else "completed",
                    verdict=coverage["verdict"], counts=coverage["counts"],
                    blocking=coverage["blocking"])
            if coverage["verdict"] == "blocked":
                for stage_id in ("S3", "S4", "S5", "S6"):
                    _finish(stages[stage_id], "skipped", reason="coverage_blocked")
                raise _PipelineBlocked("coverage")
            # S3 materialize -------------------------------------------------
            _begin(stages["S3"])
            materialize = _materialize(self.root, self.lake, coverage, values,
                                       self._progress(progress, "S3"), check_cancel,
                                       self.operation_lock)
            report["materialize"] = materialize
            report["manifest_id"] = materialize.get("manifest_id")
            _finish(stages["S3"], "completed" if not materialize["skipped"] else "skipped",
                    **{key: materialize.get(key) for key in
                       ("skipped", "reason", "manifest_id", "read_ref", "read_ref_source")})
            _guard(check_cancel)
            # S4 default method ----------------------------------------------
            _begin(stages["S4"])
            refit = _resolve_default(self.root, release, release_error, values, target,
                                     sessions, now,
                                     progress=self._progress(progress, "S4"),
                                     check_cancel=check_cancel)
            report["refit"] = refit
            _finish(stages["S4"], refit["status"], **refit)
            if refit["status"] == "blocked":
                for stage_id in ("S5", "S6"):
                    _finish(stages[stage_id], "skipped", reason="default_method_blocked")
                raise _PipelineBlocked("default_method")
            _guard(check_cancel)
            # S5 selection ---------------------------------------------------
            _begin(stages["S5"])
            scan = _run_scan(self.root, coverage, values, target,
                             self._progress(progress, "S5"), check_cancel)
            report["scan"] = scan
            report["read_ref"] = report.get("read_ref") or scan.get("read_ref")
            _finish(stages["S5"], scan["status"], **{key: scan.get(key) for key in
                                                     ("reason", "run_id", "final_ranking",
                                                      "scored", "candidates_written", "error")})
            if scan["status"] == "blocked":
                _finish(stages["S6"], "skipped", reason="scan_blocked")
                raise _PipelineBlocked("scan")
            _guard(check_cancel)
            # S6 backtest ----------------------------------------------------
            _begin(stages["S6"])
            backtest = _run_backtest(self.root, coverage, values, target, release, sessions,
                                     self._progress(progress, "S6"), check_cancel)
            report["backtest"] = backtest
            report["read_ref"] = report.get("read_ref") or backtest.get("read_ref")
            _finish(stages["S6"], backtest["status"], **{key: backtest.get(key) for key in
                                                         ("reason", "run_id", "mode", "start",
                                                          "end", "window_source",
                                                          "performance_status", "error")})
        except _PipelineBlocked:
            pass
        except JobCancelled as error:
            cancelled = True
            failed = str(error) or "cancelled"
            for stage in stages.values():
                if stage["status"] == "running":
                    _finish(stage, "cancelled", error=failed)
        except Exception as error:  # No silent success on an unexpected failure.
            failed = f"{type(error).__name__}: {error}"
            if isinstance(error, JobTerminationError):
                report.update(cancel_effective=False, cancel_note=str(error))
            for stage in stages.values():
                if stage["status"] == "running":
                    _finish(stage, "failed", error=failed)
        # Every documented field stays present even when an earlier verdict stopped the run.
        if report.get("data_update") is None:
            report["data_update"] = {"status": update.get("status", "skipped"),
                                     "reason": update.get("reason")}
        if report.get("materialize") is None:
            report["materialize"] = {"status": "skipped", "reason": "not_reached",
                                     "skipped": True}
        if report.get("refit") is None:
            report["refit"] = {"status": "skipped", "reason": "not_reached"}
        if report.get("scan") is None:
            report["scan"] = {"status": "skipped", "reason": "not_reached",
                              "candidates_written": False, "top": []}
        if report.get("backtest") is None:
            report["backtest"] = {"status": "skipped", "reason": "not_reached",
                                  "performance_status": "unavailable"}
        try:
            if cancelled:
                status, exit_code, error = "cancelled", 1, failed
            elif failed:
                status, exit_code, error = "failed", 1, failed
            else:
                status, exit_code = _decide_status(blockers, update, refit, scan, backtest,
                                                   coverage)
                error = failed
            if status == "blocked" and not error:
                reasons = {str(item.get("code") or item.get("reason"))
                           for item in blockers if item.get("code") or item.get("reason")}
                reasons |= {str(item.get("reason")) for item in coverage.get("blocking") or []
                            if item.get("reason")}
                for item in (scan, refit):
                    if item.get("reason"):
                        reasons.add(str(item["reason"]))
                error = ",".join(sorted(reason for reason in reasons if reason)) or "blocked"
            for stage in stages.values():
                if stage["status"] == "pending":
                    _finish(stage, "skipped", reason="not_reached")
            # S7 summary -----------------------------------------------------
            _begin(stages["S7"])
            report.update(status=status, exit_code=exit_code, error=error,
                          finished_at=_now(),
                          stages=[stages[key] for key in STAGE_TITLES])
            report["coverage"] = coverage
            state = {
                "schema": SCHEMA_STATE,
                "kind": report["kind"],
                "pipeline_id": pipeline_id,
                "status": status,
                "exit_code": exit_code,
                "target_date": report["target_date"],
                "params": values,
                "stages": report["stages"],
                "coverage_summary": report["coverage_summary"],
                "error": error,
                "created_at": created_at,
                "finished_at": report["finished_at"],
            }
            self._write_artifacts(pipeline_id, state, coverage, report)
            _finish(stages["S7"], "completed", pipeline_id=pipeline_id, status=status,
                    exit_code=exit_code)
            report["stages"] = [stages[key] for key in STAGE_TITLES]
            state["stages"] = report["stages"]
            self._write_artifacts(pipeline_id, state, coverage, report)
        except Exception as error:
            # Finalisation is part of the contract: never raise away the run.
            report.update(status="failed", exit_code=1, finished_at=_now(),
                          error=f"Pipeline finalisation failed: {type(error).__name__}: {error}")
        return report

    def latest(self) -> dict | None:
        directory = self.root / "var/pipeline"
        if not directory.is_dir():
            return None
        best: tuple[str, dict] | None = None
        for folder in directory.iterdir():
            if not folder.is_dir() or not PIPELINE_ID_PATTERN.fullmatch(folder.name):
                continue
            record = _read_json(folder / "report.json") or _read_json(folder / "pipeline.json")
            if not isinstance(record, dict):
                continue
            if _is_preview(record):
                continue
            stamp = str(record.get("finished_at") or record.get("created_at") or "")
            if best is None or stamp > best[0]:
                best = (stamp, record)
        return best[1] if best else None

    def read(self, pipeline_id: str) -> dict:
        if not isinstance(pipeline_id, str) or not PIPELINE_ID_PATTERN.fullmatch(pipeline_id):
            raise KeyError(pipeline_id)
        folder = self.root / "var/pipeline" / pipeline_id
        record = _read_json(folder / "report.json") or _read_json(folder / "pipeline.json")
        if not isinstance(record, dict):
            raise KeyError(pipeline_id)
        if _is_preview(record):
            return {**record, "kind": "preview", "status_label": "只读预检（历史记录）"}
        return record

    # -- internals ---------------------------------------------------------

    def _preflight(self, target: str | None, sessions: list[str], calendar_source: str,
                   calendar_error: str | None, release: dict | None,
                   release_error: str | None) -> dict:
        config_path = _config_path(self.root)
        free = _disk_free_bytes(self.root)
        checks = {
            "config": {"ok": config_path.is_file(), "path": str(config_path)},
            "calendar": {"ok": bool(sessions), "source": calendar_source,
                         "sessions": len(sessions), "error": calendar_error},
            "disk": {"ok": free >= DISK_REQUIRED_BYTES, "free_bytes": free,
                     "required_bytes": DISK_REQUIRED_BYTES},
            "active_release": {"ok": release is not None,
                               "release_id": (release or {}).get("release_id"),
                               "error": release_error},
            "target_date": {"ok": target is not None, "value": target,
                            "is_session": (target in sessions) if target and sessions else None},
        }
        blockers: list[dict] = []
        if not checks["config"]["ok"]:
            blockers.append({"code": "config_missing",
                             "message": f"缺少配置文件 {config_path}"})
        if not checks["calendar"]["ok"]:
            blockers.append({"code": "calendar_unavailable",
                             "message": f"交易日历不可用：{calendar_error or 'unknown'}"})
        if not checks["target_date"]["ok"]:
            blockers.append({"code": "target_date_unresolved",
                             "message": "无法确定目标交易日；请显式传入 end"})
        if not checks["disk"]["ok"]:
            blockers.append({"code": "disk_insufficient",
                             "message": f"剩余磁盘 {free} 字节 < 门槛 {DISK_REQUIRED_BYTES} 字节"})
        if not checks["active_release"]["ok"]:
            blockers.append({"code": "no_active_release",
                             "message": release_error or "没有可用的默认方法"})
        return {"checks": checks, "blockers": blockers, "ok": not blockers}

    def _plan(self, values: dict, coverage: dict) -> list[dict]:
        dry = values["dry_run"]
        return [
            {"id": "S0", "title": STAGE_TITLES["S0"], "will_run": True},
            {"id": "S1", "title": STAGE_TITLES["S1"],
             "will_run": not dry and not values["skip_update"]},
            {"id": "S2", "title": STAGE_TITLES["S2"], "will_run": True},
            {"id": "S3", "title": STAGE_TITLES["S3"],
             "will_run": not dry and bool(coverage.get("can_scan"))},
            {"id": "S4", "title": STAGE_TITLES["S4"], "will_run": True},
            {"id": "S5", "title": STAGE_TITLES["S5"],
             "will_run": not dry and bool(coverage.get("can_scan")) and values["scan"]},
            {"id": "S6", "title": STAGE_TITLES["S6"],
             "will_run": not dry and bool(coverage.get("can_backtest")) and values["backtest"]},
            {"id": "S7", "title": STAGE_TITLES["S7"], "will_run": True},
        ]

    def _progress(self, progress, stage_id: str):
        if progress is None:
            return None

        def forward(payload=None, **kwargs):
            data = {}
            if isinstance(payload, dict):
                data.update(payload)
            elif payload is not None:
                data["message"] = str(payload)
            data.update(kwargs)
            _notify(progress, stage=stage_id, **data)

        return forward

    def _write_artifacts(self, pipeline_id: str, state: dict, coverage: dict,
                         report: dict) -> None:
        folder = self.root / "var/pipeline" / pipeline_id
        _write_json_atomic(folder / "pipeline.json", state)
        _write_json_atomic(folder / "coverage.json", coverage)
        _write_json_atomic(folder / "report.json", report)
