import threading
import time

import pytest

from lake_equity.jobs import JobManager


def wait_finished(manager, identity):
    for _ in range(200):
        body = manager.get(identity)
        if body["status"] not in {"queued", "running", "cancelling"}:
            return body
        time.sleep(0.01)
    raise AssertionError("Job failed to finish")


def test_duplicate_request_and_result_integrity(tmp_path):
    release = threading.Event()

    def dispatch(kind, params, progress, cancelled):
        release.wait(3)
        return {"run_id": "real-run", "rows": [1, 2]}

    manager = JobManager(tmp_path, dispatch)
    try:
        first = manager.submit("backtest", {"symbols": ["600519.SH"]})
        assert manager.submit("backtest", {"symbols": ["600519.SH"]})["id"] == first["id"]
        release.set()
        assert wait_finished(manager, first["id"])["status"] == "succeeded"
        assert manager.result(first["id"])["run_id"] == "real-run"
        (tmp_path / f"{first['id']}.json").write_text("{}")
        with pytest.raises(ValueError, match="核验"):
            manager.result(first["id"])
    finally:
        release.set()
        manager.close()


def test_cancellation_is_durable_and_does_not_claim_success(tmp_path):
    def dispatch(kind, params, progress, cancelled):
        while not cancelled():
            time.sleep(0.01)
        raise RuntimeError("Cancelled by the owner")

    manager = JobManager(tmp_path, dispatch)
    job = manager.submit("scan", {})
    manager.cancel(job["id"])
    finished = wait_finished(manager, job["id"])
    assert finished["status"] == "cancelled"
    assert finished.get("cancel_effective", True) is True
    manager.close()
    reopened = JobManager(tmp_path, dispatch)
    try:
        assert reopened.get(job["id"])["status"] == "cancelled"
    finally:
        reopened.close()


def test_blocked_model_is_partial_and_preserves_its_report(tmp_path):
    manager = JobManager(tmp_path, lambda *args: {"status": "blocked", "reason": "insufficient_frozen_training_data"})
    try:
        job = manager.submit("training", {})
        assert wait_finished(manager, job["id"])["status"] == "partial"
        assert manager.result(job["id"])["status"] == "blocked"
    finally:
        manager.close()


def test_native_positional_progress_is_preserved(tmp_path):
    def dispatch(kind, params, progress, cancelled):
        progress("simulation", 5, 10, 0, date="2026-10-22")
        return {"status": "completed"}

    manager = JobManager(tmp_path, dispatch)
    try:
        job = manager.submit("backtest", {})
        final = wait_finished(manager, job["id"])
        assert final["status"] == "succeeded"
        assert final["progress"] == {"stage": "simulation", "current": 5, "total": 10, "failed": 0,
                                     "date": "2026-10-22", "progress_scope": "current_stage"}
    finally:
        manager.close()


def test_finished_non_cooperative_work_preserves_acknowledged_cancellation(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def dispatch(kind, params, progress, cancelled):
        started.set()
        release.wait(3)
        return {"status": "completed", "run_id": "finished-evidence"}

    manager = JobManager(tmp_path, dispatch)
    try:
        job = manager.submit("prediction", {})
        assert started.wait(3)
        assert manager.cancel(job["id"])["cancel_requested"] is True
        release.set()
        final = wait_finished(manager, job["id"])
        assert final["status"] == "succeeded"
        assert final["cancel_requested"] is True
        assert final["cancel_effective"] is False
        assert final["cancel_note"]
        assert manager.result(job["id"])["run_id"] == "finished-evidence"
    finally:
        release.set()
        manager.close()
