from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
import types

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from lake_equity.settings import Settings


@pytest.fixture
def integrated_app(tmp_path, monkeypatch):
    import cnequity.serve
    import lake_equity.data
    import lake_equity.research
    from lake_equity.app import create_app

    settings = Settings(tmp_path)
    settings.raw_db.parent.mkdir(parents=True)
    (tmp_path / "var" / "logs").mkdir(parents=True)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("Synthetic integration test", encoding="utf-8")
    config = tmp_path / "configs" / "cnequity.toml"
    config.parent.mkdir()
    config.write_text('[data]\nroot="' + (tmp_path / 'var/lake').as_posix() + '"\n', encoding="utf-8")
    with sqlite3.connect(settings.raw_db) as connection:
        connection.execute("CREATE TABLE sentinel(value INTEGER)")
        connection.execute("INSERT INTO sentinel VALUES(1)")
    manifest = {"manifest_id": "synthetic-input-manifest", "sqlite_sha256": hashlib.sha256(settings.raw_db.read_bytes()).hexdigest(), "status": "complete", "rows": 1}
    settings.raw_db.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(Settings, "configure", lambda self: None)
    monkeypatch.delenv("LAKE_EQUITY_API_TOKEN", raising=False)
    monkeypatch.delenv("LAKE_EQUITY_RAW_DB", raising=False)

    class FakeLake:
        def __init__(self, *args, **kwargs):
            from cnequity.config import Config

            self.config = Config(data_root=tmp_path / "var" / "lake")
            self.config.config_path = config
            self.calls = []

        def catalog(self):
            self.calls.append("catalog")
            return {"datasets": [], "count": 0}

        def quality(self):
            self.calls.append("quality")
            return {"findings_runs": [], "historical_completeness": "not_assessed"}

        def materialize_legacy(self, **kwargs):
            self.calls.append("materialize")
            return {"status": "complete", "manifest_id": "synthetic-materialized", "rows": 1}

    class FakeBridge:
        def __init__(self, **kwargs):
            self.calls = []

        def analyze(self, spec):
            self.calls.append(spec)
            return {"status": "completed", **spec}

        def close(self):
            return None

        def list_reports(self, **kwargs):
            return {"items": []}

        def read_report(self, identity):
            raise KeyError(identity)

    class FakePersonal:
        def __init__(self, *args, **kwargs):
            pass

    def native_factory(*args, **kwargs):
        raise AssertionError("Retired FastAPI applications must never be instantiated")

    workflow_calls = []

    class FakeWorkflow:
        def __init__(self, root):
            pass

        def list_runs(self, **kwargs):
            return {"reports": []}

        def scan(self, params, **kwargs):
            workflow_calls.append(("scan", params))
            return {"run_id": "scan-synthetic", "status": "completed"}

        def backtest(self, params, **kwargs):
            workflow_calls.append(("backtest", params))
            return {"run_id": "backtest-synthetic", "status": "completed"}

        def preflight(self, params):
            return {"status": "ready", "params": params}

    class FakeRegistry:
        def __init__(self, root):
            pass

        def list_models(self):
            return {"models": [], "series": []}

        def read_model(self, model_id):
            return {"model_id": model_id}

        def read_series(self, series_id):
            return {"series_id": series_id}

    workflow_module = types.ModuleType("lake_equity.learning.workflow")
    workflow_module.LearningWorkflow = FakeWorkflow
    registry_module = types.ModuleType("lake_equity.learning.registry")
    registry_module.ModelRegistry = FakeRegistry
    monkeypatch.setitem(sys.modules, "lake_equity.learning.workflow", workflow_module)
    monkeypatch.setitem(sys.modules, "lake_equity.learning.registry", registry_module)

    personal_module = types.ModuleType("my_strategy.storage.personal_portfolio")
    personal_module.PersonalStore = FakePersonal
    research_module = types.ModuleType("my_strategy.web_dashboard.app")
    research_module.create_app = native_factory
    monkeypatch.setitem(sys.modules, "my_strategy.storage.personal_portfolio", personal_module)
    monkeypatch.setitem(sys.modules, "my_strategy.web_dashboard.app", research_module)
    monkeypatch.setattr(lake_equity.data, "LakeService", FakeLake)
    monkeypatch.setattr(lake_equity.research, "ResearchBridge", FakeBridge)
    monkeypatch.setattr(cnequity.serve, "create_app", native_factory)
    app = create_app(settings)
    app.state.workflow_calls = workflow_calls
    with TestClient(app) as client:
        yield client, app, settings


@pytest.mark.parametrize("path", ["/api/lake/health?days=7", "/api/lake/docs", "/api/lake/openapi.json",
                                  "/api/khquant/health", "/native/lake", "/native/research/", "/native/research/static/app.js"])
def test_retired_apps_and_apis_are_not_mounted(integrated_app, path):
    client, app, settings = integrated_app
    assert client.get(path).status_code == 404


def test_native_research_cannot_mutate_frozen_input(integrated_app):
    client, app, settings = integrated_app
    assert client.post("/api/khquant/data/update", json={}).status_code == 404


def test_quality_request_reads_evidence_without_dispatching_work(integrated_app):
    client, app, settings = integrated_app
    result = client.get("/api/v1/quality")
    assert result.status_code == 200
    assert app.state.lake.calls == ["quality"]
    assert app.state.jobs.list() == []


def test_analysis_uses_server_input_manifest(integrated_app):
    client, app, settings = integrated_app
    result = client.post("/api/v1/analyze", json={"symbol": "600000.SH", "input_manifest_id": "untrusted-client-value"})
    assert result.status_code == 200
    assert result.json()["input_manifest_id"] == "synthetic-input-manifest"


def test_changed_sqlite_cannot_claim_its_previous_manifest(integrated_app):
    client, app, settings = integrated_app
    with sqlite3.connect(settings.raw_db) as connection:
        connection.execute("UPDATE sentinel SET value=2")
    result = client.post("/api/v1/analyze", json={"symbol": "600000.SH"})
    assert result.status_code in {400, 409, 503}
    assert app.state.research.calls == []


def test_new_model_and_series_routes_are_registry_backed(integrated_app):
    client, app, settings = integrated_app
    assert client.get("/api/v1/models/model-synthetic").json() == {"model_id": "model-synthetic"}
    assert client.get("/api/v1/learning/series/series-synthetic").json() == {"series_id": "series-synthetic"}
    assert client.post("/api/v1/learning/preflight", json={"symbols": ["600000.SH"]}).json()["status"] == "ready"


@pytest.mark.parametrize("kind", ["scan", "backtest"])
def test_model_workflows_do_not_dispatch_legacy_research(integrated_app, kind):
    client, app, settings = integrated_app
    settings.raw_db.with_suffix(".manifest.json").unlink()
    params = {"model_id": "model-synthetic", "scope": "all", "end": "2026-10-22"}
    submitted = client.post("/api/v1/jobs", json={"kind": kind, "params": params})
    assert submitted.status_code == 202
    for _ in range(200):
        result = app.state.jobs.get(submitted.json()["id"])
        if result["status"] not in {"queued", "running", "cancelling"}:
            break
        time.sleep(.01)
    assert result["status"] == "succeeded"
    assert app.state.workflow_calls == [(kind, params)]
    assert app.state.research.calls == []


def test_retired_training_kind_is_rejected_before_queue(integrated_app):
    client, app, _ = integrated_app
    response = client.post("/api/v1/jobs", json={"kind": "training", "params": {}})
    assert response.status_code == 400
    assert "factor_training" in response.json()["detail"]
    assert app.state.jobs.list() == []


def test_final_refit_is_an_explicit_new_job_without_old_training(integrated_app, monkeypatch):
    client, app, settings = integrated_app
    calls=[]
    module=types.ModuleType('lake_equity.learning.refit')
    def refit(root, request, progress=None, check_cancel=None):
        check_cancel()
        calls.append((root,request))
        return {'run_id':'learn-refit-synthetic','status':'completed','family':'frozen_factor_refit_v1'}
    module.refit_model=refit
    monkeypatch.setitem(sys.modules,'lake_equity.learning.refit',module)
    request={'run_id':'learn-original','model_name':'factor_mlp','end':'2026-10-22'}
    response=client.post('/api/v1/jobs',json={'kind':'model_refit','params':request})
    assert response.status_code == 202
    for _ in range(200):
        result=app.state.jobs.get(response.json()['id'])
        if result['status'] not in {'queued','running','cancelling'}:
            break
        time.sleep(.01)
    assert result['status'] == 'succeeded' and calls == [(settings.root,request)]
    assert app.state.research.calls == []


def test_degraded_cne_daily_is_not_promoted_by_materialization(integrated_app, monkeypatch):
    import lake_equity.app

    client, app, settings = integrated_app

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, *args, stdout, **kwargs):
            stdout.write(json.dumps({"run_id": "synthetic-cne-run", "status": "degraded", "execution_status": "completed", "coverage_status": "partial", "publication_status": "partial"}) + "\n")
            stdout.flush()

        def poll(self):
            return self.returncode

    monkeypatch.setattr(lake_equity.app.subprocess, "Popen", FakeProcess)
    submitted = client.post("/api/v1/jobs", json={"kind": "update", "params": {}})
    assert submitted.status_code == 202
    identity = submitted.json()["id"]
    for attempt in range(200):
        result = client.get(f"/api/v1/jobs/{identity}").json()
        if result["status"] not in {"queued", "running", "cancelling"}:
            break
        time.sleep(0.01)
    assert result["status"] == "partial"
    assert "synthetic-cne-run" in json.dumps(result)


def test_cancel_while_waiting_for_input_does_not_start_research(integrated_app):
    client, app, settings = integrated_app
    with FileLock(str(settings.root / "var/research_inputs/input.lock")):
        submitted = client.post("/api/v1/jobs", json={"kind": "analysis", "params": {"symbol": "600000.SH"}})
        identity = submitted.json()["id"]
        for attempt in range(200):
            if app.state.jobs.get(identity)["status"] == "running":
                break
            time.sleep(0.01)
        client.post(f"/api/v1/jobs/{identity}/cancel")
        for attempt in range(200):
            result = app.state.jobs.get(identity)
            if result["status"] == "cancelled":
                break
            time.sleep(0.01)
        assert result["status"] == "cancelled"
        assert app.state.research.calls == []
