from __future__ import annotations

import json
import time

from lake_equity.jobs import JobManager


def test_failed_job_persists_structured_error_result(tmp_path):
    def dispatch(kind, params, progress, cancelled):
        raise ValueError("synthetic failure")

    manager = JobManager(tmp_path, dispatch)
    try:
        submitted = manager.submit("pipeline", {"end": "2026-10-23"})
        for _ in range(200):
            body = manager.get(submitted["id"])
            if body["status"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(0.01)
        assert body["status"] == "failed"
        assert body["result"] is not None
        result = manager.result(submitted["id"])
        assert result["schema"] == "job_error_v1"
        assert result["status"] == "failed"
        assert result["job_id"] == submitted["id"]
        assert result["kind"] == "pipeline"
        assert "synthetic failure" in result["error"]
        saved = json.loads((tmp_path / body["result"]["path"]).read_text(encoding="utf-8"))
        assert saved["job_id"] == submitted["id"]
    finally:
        manager.close()


def test_blocked_business_result_maps_to_partial_job_status(tmp_path):
    def dispatch(kind, params, progress, cancelled):
        return {"status": "blocked", "exit_code": 4, "reason": "required coverage partial"}

    manager = JobManager(tmp_path, dispatch)
    try:
        submitted = manager.submit("pipeline", {})
        for _ in range(200):
            body = manager.get(submitted["id"])
            if body["status"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(0.01)
        assert body["status"] == "partial"
        assert body["business_status"] == "blocked"
        assert manager.result(submitted["id"])["exit_code"] == 4
    finally:
        manager.close()
