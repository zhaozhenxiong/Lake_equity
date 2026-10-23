"""A run keeps its steps' findings whether or not it ran ``audit``.

Scheduled groups run ``cne run daily --group ...`` without the audit step, and
step findings only ever reached disk through ``run_audit``. The cached
trading_status fallback reported itself on every outage day and no file held
any of it.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date

import polars as pl
import pytest

import cnequity.steps  # noqa: F401
from cnequity.config import Config
from cnequity.orchestrator.engine import JobEngine
from cnequity.orchestrator.registry import STEP_REGISTRY
from cnequity.quality.audit import persist_step_findings, run_audit
from cnequity.storage.layout import init_data_layout

TD = date(2026, 10, 9)
FINDING = {
    "check": "trading_status_cached_fallback",
    "dataset": "trading_status",
    "severity": "warning",
    "message": "EastMoney was unavailable",
    "trade_dates": [TD.isoformat()],
}


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(data_root=tmp_path / "data")
    init_data_layout(config)
    path = config.curated_root / "trading_calendar" / "part-merged.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"trade_date": [TD], "is_trading": [True]}).write_parquet(path)
    return config


@pytest.fixture
def reporting_step(monkeypatch):
    def fn(config, trade_date, run_id, context):
        return {
            "status": "warning",
            "rows_read": 0,
            "rows_written": 0,
            "context_updates": {"audit_findings": [dict(FINDING)]},
        }

    entry = STEP_REGISTRY["trading_calendar"]
    monkeypatch.setitem(STEP_REGISTRY, "trading_calendar", dataclasses.replace(entry, fn=fn))


def _persisted(cfg: Config, run_id: str) -> list[dict]:
    path = cfg.meta_root / "quality" / "findings" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))["findings"]


def test_a_run_without_audit_still_records_step_findings(cfg, reporting_step):
    result = JobEngine(cfg).run_job("daily", TD, steps=["trading_calendar"])

    assert not any(row.get("step") == "audit" for row in result["results"])
    assert _persisted(cfg, result["run_id"]) == [FINDING]


def test_persisting_a_finding_twice_records_it_once(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    other = {**FINDING, "check": "other"}

    persist_step_findings(cfg, "r1", TD, [FINDING])
    persist_step_findings(cfg, "r1", TD, [dict(FINDING), other])

    assert _persisted(cfg, "r1") == [FINDING, other]


def test_standalone_audit_preserves_steps_but_recomputes_lake_errors(cfg, monkeypatch):
    error = {"check": "lake_error", "severity": "error", "message": "broken file"}
    lake = [error]
    monkeypatch.setattr("cnequity.quality.audit._collect_lake_findings", lambda *a: list(lake))
    monkeypatch.setattr("cnequity.quality.audit.run_source_diffs", lambda *a: [])
    persist_step_findings(cfg, "r1", TD, [FINDING])
    run_audit(cfg, "r1", TD, {})
    assert _persisted(cfg, "r1") == [error, FINDING]
    lake.clear()
    context = {}
    run_audit(cfg, "r1", TD, context)
    assert _persisted(cfg, "r1") == [FINDING]
    assert context["audit_by_severity"] == {"warning": 1}


def test_multi_run_audit_gates_on_an_earlier_group_error(cfg, monkeypatch):
    error = {**FINDING, "severity": "error", "check": "earlier_group_missing"}
    persist_step_findings(cfg, "core", TD, [error])
    monkeypatch.setattr("cnequity.quality.audit._collect_lake_findings", lambda *a: [])
    monkeypatch.setattr("cnequity.quality.audit.run_source_diffs", lambda *a: [])
    context = {"related_run_ids": ["core", "research", "core"]}
    run_audit(cfg, "research", TD, context)
    assert context["audit_by_severity"] == {"error": 1}
    assert context["audit_error_findings"][0]["origin_run_id"] == "core"
    report = json.loads((cfg.meta_root / "quality/findings/research.json").read_text())
    assert report["step_findings"] == []


def test_legacy_unaudited_report_is_preserved(cfg, monkeypatch):
    path = cfg.meta_root / "quality/findings/legacy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"findings": [FINDING]}))
    monkeypatch.setattr("cnequity.quality.audit._collect_lake_findings", lambda *a: [])
    monkeypatch.setattr("cnequity.quality.audit.run_source_diffs", lambda *a: [])
    run_audit(cfg, "legacy", TD, {})
    assert _persisted(cfg, "legacy") == [FINDING]
