"""A step's EastMoney requests reach the run metrics even though the step never counts them."""

from __future__ import annotations

from datetime import date

from cnequity.config import Config
from cnequity.domain.request_meter import count_request, request_meter_scope
from cnequity.orchestrator.engine import JobEngine
from cnequity.orchestrator.registry import StepEntry


def test_count_request_is_a_no_op_outside_a_step():
    count_request()  # must not raise


def test_meter_counts_requests_and_retries_inside_its_scope():
    with request_meter_scope() as meter:
        count_request()
        count_request(retry=True)
    assert (meter.requests, meter.request_retries) == (2, 1)


def test_engine_records_requests_a_step_did_not_count(tmp_path, monkeypatch):
    from cnequity.orchestrator import engine as engine_module

    def step(config, trade_date, run_id, context):
        for _ in range(3):
            count_request()
        return {"rows_written": 0}

    entry = StepEntry(fn=step, group="core")
    monkeypatch.setattr(engine_module, "get_step", lambda name: entry)
    engine = JobEngine(Config(data_root=tmp_path / "data"))
    run_id = engine.manifest.start_run("metered", {})

    result = engine._run_step("metered", date(2026, 10, 16), run_id, {})

    assert result["metrics"]["requests"] == 3
