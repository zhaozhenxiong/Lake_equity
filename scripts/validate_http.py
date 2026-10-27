"""Check the v1 workbench; new bounded model jobs require --run-jobs."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", default="http://127.0.0.1:8130")
    result.add_argument("--output", type=Path, help="New directory for the completed acceptance report")
    result.add_argument("--release-id", help="Pin a release instead of resolving the active release once")
    result.add_argument("--run-jobs", action="store_true", help="Explicitly create one small scan and frozen backtest; never trains or activates")
    result.add_argument("--symbols", default="000001.SZ,600519.SH,002436.SZ", help="1 to 10 explicit Shanghai/Shenzhen A-share symbols")
    result.add_argument("--start", help="Required with --run-jobs; ISO date after the frozen model information cutoff")
    result.add_argument("--end", help="Required with --run-jobs; at most 31 calendar days after --start")
    result.add_argument("--job-timeout", type=float, default=240, help="Maximum polling seconds per job (1 to 600)")
    result.add_argument("--request-timeout", type=float, default=30, help="HTTP request timeout in seconds (1 to 180)")
    result.add_argument("--poll-interval", type=float, default=.25, help="Polling interval in seconds (.01 to 5)")
    return result


def job_checks(kind, value, pins, symbols):
    """Validate current model reports independently of the retired structure API."""
    cohort = value.get("cohort_effective") or {}
    checks = {
        "completed": value.get("status") == "completed",
        "family": value.get("family") == f"model_{kind}_v1",
        "frozen_release": value.get("release_id") == pins["release_id"],
        "frozen_model": value.get("model_id") == pins["model_id"],
        "pinned_read_ref": value.get("read_ref") == pins["read_ref"],
        "explicit_cohort": cohort.get("universe_scope") == "explicit_symbols" and cohort.get("requested") == len(symbols),
        "complete_cohort": cohort.get("scored") == len(symbols) and not cohort.get("excluded") and not cohort.get("unavailable"),
        "saved_identity": isinstance(value.get("run_id"), str) and value["run_id"].startswith(kind + "-"),
    }
    if kind == "scan":
        rows = value.get("rows") or []
        checks["complete_ranking"] = value.get("final_ranking") is True
        checks["requested_rows"] = len(rows) == len(symbols) and {row.get("symbol") for row in rows} == set(symbols)
    else:
        account = value.get("portfolio") or {}
        ledger = account.get("ledger")
        checks["account_schema"] = account.get("schema") == "lake_target_weight_backtest_v1"
        checks["shared_cash"] = account.get("account_scope") == "shared_cash_account"
        checks["daily_account"] = bool(account.get("daily"))
        checks["execution_rules"] = bool((account.get("execution_rules") or {}).get("version"))
        checks["ledger_contract"] = isinstance(ledger, list) and ledger == account.get("trades") and all(
            row.get("ledger_version") == 3 and {"fee", "signal_date", "cash_after", "position_after"} <= row.keys()
            for row in ledger)
    return checks


def run(arguments):
    output = arguments.output or Path(__file__).resolve().parents[1] / "var/validation" / (
        "http-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": "lake_http_acceptance_v2", "url": arguments.url,
              "mode": "bounded_model_jobs" if arguments.run_jobs else "read_only",
              "started_at": datetime.now(timezone.utc).isoformat(), "steps": {}, "pins": {}}

    def persist():
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    def request(path, body=None):
        headers = {"Accept": "application/json"}
        if token := os.environ.get("LAKE_EQUITY_API_TOKEN"):
            headers["Authorization"] = "Bearer " + token
        data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        with urlopen(Request(arguments.url.rstrip("/") + path, data=data, headers=headers), timeout=arguments.request_timeout) as response:
            return json.load(response)

    def step(name, action):
        started = time.monotonic()
        try:
            value = action()
            entry = {"status": "passed" if all(value.get("checks", {}).values()) else "failed", **value}
        except Exception as error:
            entry = {**report["steps"].get(name, {}), "status": "failed", "error_type": type(error).__name__, "error": str(error)}
        entry["elapsed_seconds"] = time.monotonic() - started
        report["steps"][name] = entry
        persist()
        return entry

    for name in ("health", "jobs", "models", "quality"):
        def read(name=name):
            value = request("/api/v1/" + name)
            checks = {"object_response": isinstance(value, dict)}
            if name == "health":
                checks["healthy"] = isinstance(value, dict) and value.get("status") == "ok"
            return {"checks": checks, "keys": sorted(value) if isinstance(value, dict) else []}
        step(name, read)

    def freeze():
        path = "/api/v1/strategy/releases/" + quote(arguments.release_id, safe="") if arguments.release_id else "/api/v1/strategy/active"
        value = request(path)
        release = value if arguments.release_id else value.get("release")
        if not release or not release.get("release_id") or not release.get("model_id"):
            raise ValueError("No frozen strategy release is available; create/activate it separately before acceptance")
        model = request("/api/v1/models/" + quote(release["model_id"], safe=""))
        report["pins"].update(release_id=release["release_id"], model_id=release["model_id"], model_version=model.get("version"))
        return {"checks": {"release_model": model.get("model_id") == release["model_id"],
                           "model_version": bool(model.get("version")) and model.get("version") == release.get("model_version")},
                "information_cutoff": model.get("information_cutoff")}
    step("frozen_method", freeze)

    def execute(kind):
        params = dict(scope="symbols", symbols=arguments.symbols, end=arguments.end,
                      universe_scope="explicit_symbols", expected_release_id=report["pins"]["release_id"],
                      **report["pins"])
        params.pop("model_version", None)
        if kind == "backtest":
            params.update(mode="frozen", start=arguments.start)
        accepted = request("/api/v1/jobs", {"kind": kind, "params": params})
        identity = accepted["id"]
        # Persist the accepted identity before polling so transport failures remain traceable.
        report["steps"][kind] = {"status": "running", "job_id": identity, "params": params}
        persist()
        deadline = time.monotonic() + arguments.job_timeout
        while True:
            job = request("/api/v1/jobs/" + quote(identity, safe=""))
            if job["status"] not in {"queued", "running", "cancelling"}:
                break
            if time.monotonic() >= deadline:
                request("/api/v1/jobs/" + quote(identity, safe="") + "/cancel", {})
                report["steps"][kind]["cancel_requested"] = True
                raise TimeoutError(f"Job {identity} exceeded the bounded wait; cancellation requested, termination not yet verified")
            time.sleep(arguments.poll_interval)
        value = request("/api/v1/jobs/" + quote(identity, safe="") + "/result")
        checks = job_checks(kind, value, report["pins"], arguments.symbols)
        checks["job_succeeded"] = job["status"] == "succeeded"
        if value.get("run_id"):
            stored = request("/api/v1/reports/" + quote(value["run_id"], safe=""))
            checks["saved_report_roundtrip"] = stored == value
        return {"checks": checks, "job_id": identity, "run_id": value.get("run_id"),
                "params": params, "business_status": value.get("status"), "cohort": value.get("cohort_effective"),
                "ledger_rows": len((value.get("portfolio") or {}).get("ledger") or []) if kind == "backtest" else None}

    if arguments.run_jobs and all(entry["status"] == "passed" for entry in report["steps"].values()):
        def preflight():
            body = dict(mode="frozen", scope="symbols", symbols=arguments.symbols, start=arguments.start, end=arguments.end,
                        universe_scope="explicit_symbols", release_id=report["pins"]["release_id"],
                        expected_release_id=report["pins"]["release_id"], model_id=report["pins"]["model_id"])
            value = request("/api/v1/backtest/preview", body)
            report["pins"]["read_ref"] = value.get("read_ref")
            return {"checks": {"ready": value.get("status") == "ready", "read_ref": bool(value.get("read_ref")),
                               "all_symbols_eligible": (value.get("counts") or {}).get("eligible") == len(arguments.symbols)},
                    "preview": value}
        preview = step("preflight", preflight)
        if preview["status"] == "passed":
            for kind in ("scan", "backtest"):
                if step(kind, lambda kind=kind: execute(kind))["status"] != "passed":
                    break
    for kind in ("scan", "backtest"):
        report["steps"].setdefault(kind, {"status": "not_requested" if not arguments.run_jobs else "blocked",
                                        "reason": "Use --run-jobs with an explicit bounded date range" if not arguments.run_jobs else "A prerequisite failed; no job submitted"})
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = all(entry["status"] in {"passed", "not_requested"} for entry in report["steps"].values())
    persist()
    return report, output / "summary.json"


def main(argv=None):
    arguments_parser = parser()
    arguments = arguments_parser.parse_args(argv)
    arguments.symbols = list(dict.fromkeys(item.strip().upper() for item in arguments.symbols.split(",") if item.strip()))
    if not 1 <= len(arguments.symbols) <= 10 or any(not re.fullmatch(r"(?:6\d{5}\.SH|[03]\d{5}\.SZ)", item) for item in arguments.symbols):
        arguments_parser.error("Provide 1 to 10 Shanghai/Shenzhen A-share symbols")
    if not 1 <= arguments.job_timeout <= 600 or not 1 <= arguments.request_timeout <= 180 or not .01 <= arguments.poll_interval <= 5:
        arguments_parser.error("Timeout or polling interval is outside the documented bound")
    if arguments.run_jobs:
        try:
            start, end = date.fromisoformat(arguments.start), date.fromisoformat(arguments.end)
            if start.isoformat() != arguments.start or end.isoformat() != arguments.end or not 0 <= (end - start).days <= 31:
                raise ValueError
        except (TypeError, ValueError):
            arguments_parser.error("--run-jobs requires ISO --start/--end spanning at most 31 calendar days")
    report, summary = run(arguments)
    print(json.dumps({"passed": report["passed"], "mode": report["mode"], "summary": str(summary)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
