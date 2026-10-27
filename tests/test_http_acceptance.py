"""Real local HTTP fixtures exercise the acceptance CLI without user data or jobs."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/validate_http.py"
RELEASE = "release-" + "a" * 32
MODEL = "model-" + "b" * 32
READ_REF = "c" * 64
SYMBOLS = ["000001.SZ", "600519.SH"]


@pytest.fixture
def workbench():
    state = {"calls": [], "reports": {}, "failure_path": None, "corrupt": None, "block_preview": False, "hang": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200):
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            state["calls"].append(("GET", self.path, None))
            if self.path == state["failure_path"]:
                return self.reply({"detail": "fixture service unavailable"}, 503)
            if self.path in {"/api/v1/health", "/api/v1/quality", "/api/v1/jobs", "/api/v1/models"}:
                return self.reply({"status": "ok"})
            if self.path == "/api/v1/strategy/active":
                return self.reply({"release": {"release_id": RELEASE, "model_id": MODEL, "model_version": "version-frozen"}})
            if self.path == "/api/v1/models/" + MODEL:
                return self.reply({"model_id": MODEL, "version": "version-frozen", "information_cutoff": "2026-10-21"})
            if self.path.startswith("/api/v1/jobs/job-"):
                kind = self.path.split("job-", 1)[1].split("/")[0]
                if self.path.endswith("/result"):
                    return self.reply(state["reports"][kind])
                return self.reply({"id": "job-" + kind, "status": "running" if state["hang"] else "succeeded"})
            if self.path.startswith("/api/v1/reports/"):
                kind = self.path.rsplit("/", 1)[1].split("-", 1)[0]
                return self.reply(state["reports"][kind])
            return self.reply({"detail": "unknown fixture path"}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(("POST", self.path, body))
            if self.path == "/api/v1/backtest/preview":
                return self.reply({"status": "blocked" if state["block_preview"] else "ready", "read_ref": READ_REF, "counts": {"eligible": len(SYMBOLS)}})
            if self.path.endswith("/cancel"):
                return self.reply({"status": "cancelling"})
            if self.path != "/api/v1/jobs":
                return self.reply({"detail": "unexpected write"}, 400)
            kind, params = body["kind"], body["params"]
            value = {"run_id": kind + "-fixture", "family": "model_" + kind + "_v1", "status": "completed",
                     "release_id": params["release_id"], "model_id": params["model_id"], "read_ref": params["read_ref"],
                     "cohort_effective": {"universe_scope": params["universe_scope"], "requested": len(params["symbols"]),
                                          "scored": len(params["symbols"]), "excluded": [], "unavailable": []}}
            if kind == "scan":
                value.update(final_ranking=True, rows=[{"symbol": symbol} for symbol in params["symbols"]])
            else:
                trade = {"ledger_version": 3, "fee": 5., "signal_date": params["start"], "cash_after": 95000., "position_after": 100}
                value["portfolio"] = {"schema": "lake_target_weight_backtest_v1", "account_scope": "shared_cash_account",
                                      "daily": [{"date": params["end"], "equity": 100000.}], "execution_rules": {"version": "fixture-rules"},
                                      "ledger": [trade], "trades": [deepcopy(trade)]}
            if state["corrupt"] == "read_ref":
                value["read_ref"] = "d" * 64
            if state["corrupt"] == "cohort":
                value["cohort_effective"]["scored"] = 0
            if state["corrupt"] == "ledger" and kind == "backtest":
                value["portfolio"]["ledger"][0]["ledger_version"] = 2
            state["reports"][kind] = value
            return self.reply({"id": "job-" + kind}, 202)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def invoke(tmp_path, workbench, *extra):
    url, state = workbench
    output = tmp_path / "acceptance"
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--url", url, "--output", str(output),
                             "--symbols", ",".join(SYMBOLS), *extra], capture_output=True, text=True, timeout=15)
    return result, json.loads((output / "summary.json").read_text(encoding="utf-8"))


def test_default_acceptance_is_read_only_and_explicitly_does_not_run_business_jobs(tmp_path, workbench):
    result, report = invoke(tmp_path, workbench)
    assert result.returncode == 0, result.stderr
    assert report["passed"] and report["mode"] == "read_only" and report["finished_at"]
    assert report["steps"]["scan"]["status"] == report["steps"]["backtest"]["status"] == "not_requested"
    assert all(method == "GET" and path.startswith("/api/v1/") for method, path, body in workbench[1]["calls"])


def test_opt_in_reaches_scan_and_backtest_and_checks_current_frozen_contracts(tmp_path, workbench):
    result, report = invoke(tmp_path, workbench, "--run-jobs", "--start", "2026-10-22", "--end", "2026-10-23")
    assert result.returncode == 0, result.stderr
    assert report["mode"] == "bounded_model_jobs" and report["pins"]["read_ref"] == READ_REF
    for kind in ("scan", "backtest"):
        entry = report["steps"][kind]
        assert entry["status"] == "passed" and all(entry["checks"].values())
        assert entry["params"]["model_id"] == MODEL and entry["params"]["expected_release_id"] == RELEASE
        assert entry["checks"]["saved_report_roundtrip"]
        assert not {"analysis_method", "cpu_workers", "budget_seconds"} & entry["params"].keys()
    assert report["steps"]["backtest"]["ledger_rows"] == 1
    assert [body["kind"] for method, path, body in workbench[1]["calls"] if path == "/api/v1/jobs" and method == "POST"] == ["scan", "backtest"]


@pytest.mark.parametrize("corrupt", ["read_ref", "cohort", "ledger"])
def test_incorrect_business_result_fails_with_complete_report(tmp_path, workbench, corrupt):
    workbench[1]["corrupt"] = corrupt
    result, report = invoke(tmp_path, workbench, "--run-jobs", "--start", "2026-10-22", "--end", "2026-10-23")
    assert result.returncode == 1
    assert report["passed"] is False and report["finished_at"]
    assert any(step["status"] == "failed" for step in report["steps"].values())


def test_http_failure_is_saved_and_prevents_job_submission(tmp_path, workbench):
    workbench[1]["failure_path"] = "/api/v1/quality"
    result, report = invoke(tmp_path, workbench, "--run-jobs", "--start", "2026-10-22", "--end", "2026-10-23")
    assert result.returncode == 1 and report["finished_at"]
    assert report["steps"]["quality"]["error_type"] == "HTTPError"
    assert report["steps"]["scan"]["status"] == "blocked"
    assert not any(method == "POST" for method, path, body in workbench[1]["calls"])


def test_blocked_preflight_never_creates_jobs(tmp_path, workbench):
    workbench[1]["block_preview"] = True
    result, report = invoke(tmp_path, workbench, "--run-jobs", "--start", "2026-10-22", "--end", "2026-10-23")
    assert result.returncode == 1 and report["steps"]["preflight"]["status"] == "failed"
    assert report["steps"]["backtest"]["status"] == "blocked"
    assert not any(method == "POST" and path == "/api/v1/jobs" for method, path, body in workbench[1]["calls"])


def test_job_timeout_preserves_identity_and_requests_cancellation(tmp_path, workbench):
    workbench[1]["hang"] = True
    result, report = invoke(tmp_path, workbench, "--run-jobs", "--start", "2026-10-22", "--end", "2026-10-23",
                            "--job-timeout", "1", "--poll-interval", ".05")
    assert result.returncode == 1 and report["finished_at"]
    assert report["steps"]["scan"]["job_id"] == "job-scan"
    assert report["steps"]["scan"]["error_type"] == "TimeoutError"
    assert any(path == "/api/v1/jobs/job-scan/cancel" for method, path, body in workbench[1]["calls"])


@pytest.mark.parametrize("extra", [["--run-jobs"], ["--run-jobs", "--start", "2020-01-01", "--end", "2026-10-23"], ["--symbols", "830799.BJ"]])
def test_unbounded_or_invalid_job_requests_fail_before_contacting_the_server(tmp_path, workbench, extra):
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--url", workbench[0], "--output", str(tmp_path / "unused"), *extra],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and not workbench[1]["calls"]
    assert not (tmp_path / "unused").exists()
