"""The audit gate: what a run does when the lake audit reports errors.

`step_audit` depends on `compact`, so the rows are already in curated by the
time it runs. It recorded `success` unconditionally, which is why a run whose
audit found errors still reported a clean day. `aggregate_run_status` already
fails a run on a failed core step, so the only missing piece was this status.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from cnequity.config import Config
from cnequity.steps import finalize


@pytest.fixture
def recorded(monkeypatch):
    calls: list[dict] = []

    def _record(config, run_id, dataset, step, status, **kwargs):
        calls.append({"dataset": dataset, "status": status, **kwargs})

    monkeypatch.setattr(finalize, "_record_dataset_result", _record)
    return calls


def _audit_returning(monkeypatch, *, errors: int, warnings: int = 0):
    def _run_audit(config, run_id, trade_date, context=None):
        if context is not None:
            context["audit_by_severity"] = {"error": errors, "warning": warnings}
        return errors + warnings

    monkeypatch.setattr("cnequity.quality.audit.run_audit", _run_audit)


def _run(cfg, monkeypatch, *, errors: int) -> dict:
    _audit_returning(monkeypatch, errors=errors)
    return finalize.step_audit(cfg, date(2026, 1, 5), "run-1", {})


def _verdicts(cfg) -> list[dict]:
    path = cfg.meta_root / "quality" / "audit_gate.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_shadow_records_the_verdict_without_failing_the_run(tmp_path, monkeypatch, recorded):
    cfg = Config(data_root=tmp_path / "data", audit_gate="shadow")
    _run(cfg, monkeypatch, errors=3)

    assert recorded[-1]["status"] == "success", "shadow must not change the outcome"
    verdicts = _verdicts(cfg)
    assert len(verdicts) == 1
    assert verdicts[0]["gate"] == "shadow"
    assert verdicts[0]["blocked"] is False
    assert verdicts[0]["by_severity"]["error"] == 3


def test_block_fails_the_run_so_aggregate_status_can_see_it(tmp_path, monkeypatch, recorded):
    cfg = Config(data_root=tmp_path / "data", audit_gate="block")
    _run(cfg, monkeypatch, errors=1)

    assert recorded[-1]["status"] == "failed"
    assert recorded[-1]["criticality"] == "core", "core is what fails the run"
    assert _verdicts(cfg)[0]["blocked"] is True


def test_off_records_nothing(tmp_path, monkeypatch, recorded):
    cfg = Config(data_root=tmp_path / "data", audit_gate="off")
    _run(cfg, monkeypatch, errors=5)

    assert recorded[-1]["status"] == "success"
    assert _verdicts(cfg) == []


@pytest.mark.parametrize("gate", ["off", "shadow", "block"])
def test_a_clean_audit_is_never_recorded_or_blocked(tmp_path, monkeypatch, recorded, gate):
    cfg = Config(data_root=tmp_path / "data", audit_gate=gate)
    _run(cfg, monkeypatch, errors=0)

    assert recorded[-1]["status"] == "success"
    assert _verdicts(cfg) == [], "no errors means no verdict to record"


def test_warnings_alone_do_not_block(tmp_path, monkeypatch, recorded):
    """Only `error` gates. 32 checks emit warnings; gating on them blocks every day."""
    cfg = Config(data_root=tmp_path / "data", audit_gate="block")
    _audit_returning(monkeypatch, errors=0, warnings=9)
    finalize.step_audit(cfg, date(2026, 1, 5), "run-1", {})

    assert recorded[-1]["status"] == "success"
    assert _verdicts(cfg) == []


def test_config_rejects_an_unknown_gate(tmp_path):
    from cnequity.config.loader import validate_config

    cfg = Config(data_root=tmp_path / "data", audit_gate="maybe")
    assert any("audit_gate" in message for message in validate_config(cfg))


def test_coverage_errors_do_not_fail_the_ingestion_audit(tmp_path, monkeypatch, recorded):
    cfg = Config(data_root=tmp_path / "data", audit_gate="block")

    def audit(config, run_id, trade_date, context):
        context["audit_by_severity"] = {"error": 2}
        context["audit_error_findings"] = [{"check": "exists"}, {"check": "non_empty"}]
        return 2

    monkeypatch.setattr("cnequity.quality.audit.run_audit", audit)
    result = finalize.step_audit(cfg, date(2026, 1, 5), "run-1", {})
    assert result["status"] == "warning"
    assert recorded[-1]["status"] == "warning"
    assert not _verdicts(cfg)


def test_ingestion_audit_returns_a_real_integrity_failure_to_the_engine(tmp_path, monkeypatch):
    from cnequity.orchestrator.engine import JobEngine

    cfg = Config(data_root=tmp_path / "data", audit_gate="block")

    def audit(config, run_id, trade_date, context):
        context["audit_by_severity"] = {"error": 1}
        context["audit_error_findings"] = [{"check": "duplicate_keys"}]
        return 1

    monkeypatch.setattr("cnequity.quality.audit.run_audit", audit)
    result = JobEngine(cfg).run_job("backfill", date(2026, 1, 5), steps=["audit"])
    assert result["status"] == "failed"


def test_historical_factor_errors_are_advisory_but_invalid_factors_still_block(
    tmp_path, monkeypatch, recorded
):
    cfg = Config(data_root=tmp_path / "data", audit_gate="block")
    items = [{"dataset": "adj_factors", "check": "adj_factor_continuity", "severity": "error"}]

    def audit(config, run_id, trade_date, context):
        context["audit_by_severity"] = {"error": len(items)}
        context["audit_error_findings"] = list(items)
        return len(items)

    monkeypatch.setattr("cnequity.quality.audit.run_audit", audit)
    finalize.step_audit(cfg, date(2026, 1, 5), "semantic", {})
    assert recorded[-1]["status"] != "failed"
    items.append({"dataset": "adj_factors", "check": "invalid_values", "severity": "error"})
    finalize.step_audit(cfg, date(2026, 1, 5), "structural", {})
    assert recorded[-1]["status"] == "failed"
