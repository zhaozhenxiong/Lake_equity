from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
from datetime import datetime
import hmac
import json
import os
from pathlib import Path
from typing import Literal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from filelock import FileLock, Timeout

from lake_equity.jobs import JobCancelled, JobManager
from lake_equity.settings import Settings


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    params: dict = Field(default_factory=dict)


class WatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    settings.configure()
    from lake_equity.data import InputContract, LakeService
    from lake_equity.research import ResearchBridge
    from my_strategy.storage.personal_portfolio import PersonalStore
    from lake_equity.history import HistoryReader
    from lake_equity.operations import DataOperations, create_operations_router
    from lake_equity.data_views import create_data_views_router

    lake = LakeService(settings.root, source_db=settings.source_db)
    research = ResearchBridge(raw_db=settings.raw_db, research_root=settings.metadata_root / "light_research",
                              results_db=settings.data_root / "processed/czsc/results.db", runs_root=settings.artifact_root / "runs")
    personal = PersonalStore(settings.metadata_root / "personal_portfolio.db", raw_db=settings.raw_db)
    operation_lock = threading.Lock()
    input_contract = InputContract(settings.raw_db)
    input_lock_path = settings.root / "var/research_inputs/input.lock"
    history = HistoryReader(settings, research, lake.config)
    operations = DataOperations(lake.config, root=settings.root, busy=operation_lock.locked)

    def learning():
        from lake_equity.learning.service import LearningService
        return LearningService(settings.root)

    def registry():
        from lake_equity.learning.registry import ModelRegistry
        return ModelRegistry(settings.root)

    def workflow():
        from lake_equity.learning.workflow import LearningWorkflow
        return LearningWorkflow(settings.root)

    def with_input(params):
        requested = dict(params)
        binding = input_contract.validate()
        requested["input_manifest_id"] = binding.manifest_id
        return requested

    def run_daily(progress, cancelled):
        config_path = settings.root / "configs/cnequity.toml"
        if not config_path.is_file():
            raise ValueError("缺少 configs/cnequity.toml；请先完成本地导入与配置")
        path = settings.root / "var/logs/cne-daily.log"
        command = [sys.executable, "-m", "cnequity", "run", "daily", "--config", str(config_path)]
        with path.open("a", encoding="utf-8") as output:
            start_offset = output.tell()
            process = subprocess.Popen(command, cwd=settings.root, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            while process.poll() is None:
                if cancelled():
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise JobCancelled("日更已取消，已发布批次保留")
                progress({"phase": "cne_daily", "pid": process.pid, "log": path.name})
                time.sleep(1)
        if process.returncode:
            raise RuntimeError(f"CNEquity 日更失败（退出码 {process.returncode}）：" + path.read_text(encoding="utf-8", errors="replace")[-5000:])
        with path.open(encoding="utf-8", errors="replace") as output:
            output.seek(start_offset)
            logged = output.read()
        decoder = json.JSONDecoder()
        outcomes = []

        def inspect_outcome(value):
            if isinstance(value, dict):
                if "execution_status" in value or "coverage_status" in value or ("run_id" in value and "status" in value):
                    outcomes.append(value)
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        inspect_outcome(child)
            elif isinstance(value, list):
                for child in value:
                    inspect_outcome(child)

        offset = 0
        while offset < len(logged):
            offset = logged.find("{", offset)
            if offset < 0:
                break
            try:
                value, length = decoder.raw_decode(logged[offset:])
                inspect_outcome(value)
                offset += length
            except json.JSONDecodeError:
                offset += 1
        partial = not outcomes or any(str(value.get(field, "")).lower() in {"warning", "degraded", "partial", "pending", "unknown", "failed", "blocked", "incomplete"}
                                      for value in outcomes for field in ("status", "execution_status", "coverage_status", "publication_status"))
        materialized = lake.materialize_legacy(progress=progress, check_cancel=cancelled)
        return {"status": "partial" if partial else "complete", "cne_outcomes": outcomes, "materialize": materialized,
                "manifest_id": materialized.get("manifest_id"), "outcome_assessed": bool(outcomes), "log": path.name}

    def dispatch_unlocked(kind, params, progress, cancelled):
        def check_cancel():
            if cancelled():
                raise JobCancelled("任务已取消")
            return False

        if kind == "factor_training":
            check_cancel()
            return learning().run(params, progress=progress, check_cancel=check_cancel)
        if kind == "model_refit":
            from lake_equity.learning.refit import refit_model
            check_cancel()
            return refit_model(settings.root, params, progress=progress, check_cancel=check_cancel)
        if kind in {"scan", "backtest"}:
            check_cancel()
            return getattr(workflow(), kind)(params, progress=progress, check_cancel=check_cancel)

        while not operation_lock.acquire(timeout=0.2):
            check_cancel()
        try:
            check_cancel()
            if kind == "import":
                imported = lake.import_legacy(progress=progress, check_cancel=check_cancel)
                materialized = lake.materialize_legacy(progress=progress, check_cancel=check_cancel)
                return {"import": imported, "materialize": materialized, "manifest_id": materialized.get("manifest_id")}
            if kind == "materialize":
                return lake.materialize_legacy(progress=progress, check_cancel=check_cancel)
            if kind == "update":
                return run_daily(progress, cancelled)
            input_lock = FileLock(str(input_lock_path))
            while True:
                check_cancel()
                try:
                    input_lock.acquire(timeout=0.2)
                    break
                except Timeout:
                    continue
            try:
                check_cancel()
                return dispatch_research(kind, params, progress, check_cancel)
            finally:
                input_lock.release()
        finally:
            operation_lock.release()

    def dispatch_research(kind, params, progress, check_cancel):
        spec = with_input(params)
        if kind == "prediction":
            return research.predict(spec)
        if kind == "analysis":
            return research.analyze(spec)
        if kind == "board_features":
            from my_strategy.services.board_fluent import build_board_fluent
            return build_board_fluent(price_date=params.get("end"), with_native=params.get("with_native", True),
                                      source_root=settings.research_app, progress=progress, check_cancel=check_cancel)
        raise ValueError("不支持的任务类型")

    def dispatch(kind, params, progress, cancelled):
        if not operations.gate.enter():
            raise ValueError("Storage maintenance is active; submit after it finishes")
        try:
            return dispatch_unlocked(kind, params, progress, cancelled)
        finally:
            operations.gate.leave()

    jobs = JobManager(settings.root / "var/jobs", dispatch)
    operations.active_jobs = lambda: any(row["status"] in {"queued", "running", "cancelling"} for row in jobs.list())

    def chronological(row):
        try:
            if isinstance(row.get("created_at"), (float, int)):
                return float(row["created_at"])
            value = datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
            return (value if value.tzinfo else value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))).timestamp()
        except ValueError:
            return 0

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(jobs.close)
        await asyncio.to_thread(research.close)

    app = FastAPI(title="LakeEquity 研究工作台", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.jobs = jobs
    from lake_equity.market.routes import create_market_router

    app.include_router(create_market_router(settings.root))
    app.include_router(create_operations_router(operations))
    app.state.lake = lake
    app.state.research = research
    app.state.history = history
    app.state.operations = operations

    @app.middleware("http")
    async def security(request, call_next):
        if any(request.url.path == prefix or request.url.path.startswith(prefix + "/")
               for prefix in ("/api/khquant", "/api/lake", "/native")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        token = os.environ.get("LAKE_EQUITY_API_TOKEN", "")
        if request.url.path.startswith("/api/") and token:
            if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
                return JSONResponse({"detail": "连接密钥无效"}, status_code=401)
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "拒绝跨来源写请求"}, status_code=403)
        gated = request.url.path.startswith("/api/v1/") and not request.url.path.startswith("/api/v1/data/storage")
        if gated and not operations.gate.enter():
            return JSONResponse({"detail": "Storage maintenance is active; retry after it finishes"}, status_code=409)
        try:
            response = await call_next(request)
        finally:
            if gated:
                operations.gate.leave()
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"detail": "找不到指定任务或报告"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def unavailable(request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.exception_handler(Timeout)
    async def locked_input(request, error):
        return JSONResponse({"detail": "研究输入正在发布或被另一进程使用，请稍后重试。"}, status_code=409)

    from cnequity.serve.ops.runner import OpsRejected
    from cnequity.storage.lifecycle import LifecycleError
    from cnequity.storage.revisions import RevisionConsistencyError

    @app.exception_handler(OpsRejected)
    @app.exception_handler(LifecycleError)
    @app.exception_handler(RevisionConsistencyError)
    async def operation_conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "version": "0.1.0", "project_root": str(settings.root)}

    @app.get("/api/v1/overview")
    def overview():
        from my_strategy.adapters.czsc_adapter import data_status
        data = data_status(db_path=settings.raw_db) if settings.raw_db.is_file() else {"available": False}
        listed = jobs.list()
        return {"data": data, "lake": lake.catalog(), "research": {"available": settings.raw_db.is_file(), "methods": research.methods(), "models": registry().list_models()},
                "jobs": {"active": sum(row["status"] in {"queued", "running", "cancelling"} for row in listed), "total": len(listed)}}

    @app.get("/api/v1/catalog")
    def catalog():
        return lake.catalog()

    @app.get("/api/v1/quality")
    def quality():
        return lake.quality()

    @app.get("/api/v1/datasets/{dataset}/read")
    def read_dataset(dataset: str, symbol: str | None = None, start: str | None = None, end: str | None = None,
                     as_of: str | None = None, limit: int = Query(100, ge=1, le=1000),
                     adjust: Literal["hfq", "qfq", "total_return"] | None = None,
                     pit_mode: Literal["strict", "best_effort"] = "strict"):
        return lake.read(dataset, symbol=symbol, start=start, end=end, as_of=as_of, limit=limit,
                         adjust=adjust, pit_mode=pit_mode)

    @app.get("/api/v1/symbols")
    def symbols(q: str = "", limit: int = Query(100, ge=1, le=6000)):
        from my_strategy.core.market_scope import supported_catalog
        return {"symbols": supported_catalog(query=q or None, limit=limit, db_path=settings.raw_db)} if settings.raw_db.is_file() else {"symbols": []}

    @app.get("/api/v1/methods")
    def methods():
        return research.methods()

    @app.get("/api/v1/features")
    def features(symbol: str, end: str | None = None, profile: str = "ma_trend_v1"):
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(409, "已有数据或研究计算正在执行，请在任务页查看进度")
        try:
            with FileLock(str(input_lock_path), timeout=0):
                binding = input_contract.validate()
                result = research.features(symbol, end, profile=profile, input_manifest_id=binding.manifest_id)
                if input_contract.validate().manifest_id != binding.manifest_id:
                    raise ValueError("研究输入在计算期间发生变化")
                return result
        finally:
            operation_lock.release()

    @app.post("/api/v1/analyze")
    def analyze(spec: dict):
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(409, "已有数据或研究计算正在执行，请在任务页查看进度")
        try:
            with FileLock(str(input_lock_path), timeout=0):
                requested = with_input(spec)
                result = research.analyze(requested)
                if input_contract.validate().manifest_id != requested["input_manifest_id"]:
                    raise ValueError("研究输入在计算期间发生变化")
                return result
        finally:
            operation_lock.release()

    @app.get("/api/v1/jobs")
    def list_jobs(limit: int = Query(100, ge=1, le=1000)):
        combined = jobs.list(limit) + history.jobs(limit)
        return {"jobs": sorted(combined, key=chronological, reverse=True)[:limit]}

    @app.post("/api/v1/jobs", status_code=202)
    def submit_job(body: JobRequest):
        if body.kind == "training":
            raise ValueError("旧20维训练入口已退役，请使用统一 factor_training 与显式特征配置")
        if body.kind not in {"backtest", "scan", "prediction", "factor_training", "model_refit", "update", "import", "materialize", "analysis", "board_features"}:
            raise ValueError("不支持的任务类型")
        return jobs.submit(body.kind, body.params)

    @app.get("/api/v1/jobs/{identity}")
    def get_job(identity: str):
        if ":" in identity:
            return history.job(identity)
        body = jobs.get(identity)
        if body.get("result"):
            body["result"] = jobs.result(identity)
        return body

    @app.get("/api/v1/jobs/{identity}/result")
    def get_result(identity: str):
        if ":" in identity:
            result = history.job(identity).get("result")
            if result is None:
                raise KeyError(identity)
            return result
        return jobs.result(identity)

    @app.post("/api/v1/jobs/{identity}/cancel")
    def cancel_job(identity: str):
        if ":" in identity:
            provider, source_id = identity.split(":", 1)
            if provider == "cnequity":
                operations.ops.cancel(source_id, requested_by="LakeEquity local user")
            elif provider in {"khquant", "light"}:
                raise HTTPException(409, "历史任务仅保留读取，不启动、恢复或修改旧任务运行器")
            else:
                raise KeyError(identity)
            return history.job(identity)
        return jobs.cancel(identity)

    @app.get("/api/v1/reports")
    def reports(kind: str | None = None, limit: int = Query(100, ge=1, le=200)):
        items = history.reports(kind=kind, limit=limit)["reports"]
        if kind in {None, "factor_training", "training"}:
            items += [{**row, "kind": "factor_training"} for row in learning().list_runs().get("runs", [])]
        items += workflow().list_runs(kind=kind, limit=limit).get("reports", [])
        unique = {row.get("run_id", row.get("id")): row for row in items}
        return {"reports": sorted(unique.values(), key=chronological, reverse=True)[:limit]}

    @app.get("/api/v1/reports/{identity}")
    def report(identity: str):
        if identity.startswith("learn-"):
            return learning().read_run(identity)
        if identity.startswith(("scan-", "backtest-")):
            return workflow().read_run(identity)
        return research.read_report(identity)

    @app.get("/api/v1/models")
    def models():
        from my_strategy.services.kronos_assets import asset_status
        return {**registry().list_models(), "assets": asset_status()}

    @app.get("/api/v1/models/{model_id}")
    def model(model_id: str):
        return registry().read_model(model_id)

    @app.get("/api/v1/predictions/{reference}/chart")
    def prediction_chart(reference: str):
        return history.prediction_chart(reference)

    @app.get("/api/v1/accounts")
    @app.get("/api/v1/history/accounts")
    def historical_accounts():
        return history.accounts()

    @app.get("/api/v1/accounts/chart-options")
    def account_chart_options(symbol: str, as_of: str):
        return history.account_options(symbol, as_of)

    @app.get("/api/v1/accounts/{result_id}/chart")
    def historical_account_chart(result_id: str, symbol: str, strategy: str, as_of: str):
        return history.account_chart(result_id, symbol, strategy, as_of)

    @app.get("/api/v1/learning/catalog")
    def learning_catalog():
        return learning().catalog()

    @app.get("/api/v1/learning/runs")
    def learning_runs():
        return learning().list_runs()

    @app.get("/api/v1/learning/runs/{run_id}")
    def learning_run(run_id: str):
        return learning().read_run(run_id)

    @app.get("/api/v1/learning/series/{series_id}")
    def learning_series(series_id: str):
        return registry().read_series(series_id)

    @app.post("/api/v1/learning/preflight")
    def learning_preflight(spec: dict):
        return workflow().preflight(spec)

    @app.get("/api/v1/watchlist")
    def watchlist():
        return {"symbols": personal.watchlist()["items"]}

    @app.post("/api/v1/watchlist")
    def add_watchlist(body: WatchRequest):
        personal.add_watchlist([body.symbol])
        return watchlist()

    @app.delete("/api/v1/watchlist/{symbol}")
    def remove_watchlist(symbol: str):
        personal.remove_watchlist(symbol)
        return watchlist()

    @app.get("/api/v1/provenance")
    def provenance():
        return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in (settings.root / "docs/provenance").glob("*.json")}

    app.include_router(create_data_views_router(lake.config))
    app.mount("/assets", StaticFiles(directory=settings.root / "frontend"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(settings.root / "frontend/index.html", headers={"Cache-Control": "no-store"})

    return app
