from __future__ import annotations

import sys
import threading
import time
import types

import pytest
from filelock import FileLock

from test_api_integration import integrated_app  # noqa: F401


@pytest.fixture
def learning_api(integrated_app, monkeypatch):
    client, app, settings = integrated_app
    calls = []
    started = threading.Event()

    class FakeLearning:
        def __init__(self, root):
            assert root == settings.root

        def catalog(self):
            calls.append("catalog")
            return {"family": "cross_sectional_return_v1", "backends": [{"id": "local", "available": True}]}

        def list_runs(self):
            calls.append("list_runs")
            return {"runs": [{"run_id": "learn-synthetic", "status": "completed"}]}

        def read_run(self, run_id):
            calls.append(("read_run", run_id))
            if run_id == "missing":
                raise KeyError(run_id)
            if run_id == "tampered":
                raise ValueError("报告 SHA256 核验失败")
            return {"run_id": run_id, "status": "completed", "family": "cross_sectional_return_v1"}

        def run(self, spec, progress=None, check_cancel=None):
            check_cancel = check_cancel or (lambda: False)
            calls.append(("run", spec.copy()))
            progress({"stage": "synthetic_learning"})
            started.set()
            if spec.get("wait_for_cancel"):
                while True:
                    check_cancel()
                    time.sleep(0.005)
            check_cancel()
            return {"run_id": "learn-synthetic", "lake_run_id": "learn-synthetic", "status": "completed", "family": "cross_sectional_return_v1"}

    module = types.ModuleType("lake_equity.learning.service")
    module.LearningService = FakeLearning
    monkeypatch.setitem(sys.modules, "lake_equity.learning.service", module)
    yield client, app, settings, calls, started


def terminal(client, identity):
    for _ in range(300):
        job = client.get(f"/api/v1/jobs/{identity}").json()
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.01)
    pytest.fail("Learning job did not reach a terminal state")


def test_learning_gets_only_read_catalog_and_verified_saved_reports(learning_api):
    client, app, settings, calls, started = learning_api
    assert client.get("/api/v1/learning/catalog").json()["family"] == "cross_sectional_return_v1"
    assert client.get("/api/v1/learning/runs").json()["runs"][0]["run_id"] == "learn-synthetic"
    assert client.get("/api/v1/learning/runs/learn-synthetic").json()["run_id"] == "learn-synthetic"
    assert calls == ["catalog", "list_runs", ("read_run", "learn-synthetic")]
    assert app.state.jobs.list() == []
    assert not started.is_set()
    assert client.get("/api/v1/learning/runs/missing").status_code == 404
    assert client.get("/api/v1/learning/runs/tampered").status_code == 400


def test_factor_job_bypasses_legacy_input_lock_and_manifest(learning_api):
    client, app, settings, calls, started = learning_api
    settings.raw_db.with_suffix(".manifest.json").unlink()
    spec = {"symbols": ["600519.SH", "000001.SZ"], "start": "2024-01-01", "end": "2026-10-22",
            "factor_groups": ["price_volume", "margin"], "backend": "local", "horizon": 5}
    with FileLock(str(settings.root / "var/research_inputs/input.lock")):
        response = client.post("/api/v1/jobs", json={"kind": "factor_training", "params": spec})
        assert response.status_code == 202
        job = terminal(client, response.json()["id"])
    assert job["status"] == "succeeded"
    assert job["result"]["run_id"] == "learn-synthetic"
    assert calls == [("run", spec)]
    assert app.state.research.calls == []
    assert client.get(f"/api/v1/jobs/{job['id']}/result").json()["family"] == "cross_sectional_return_v1"


def test_learning_job_responds_to_existing_cancel_endpoint(learning_api):
    client, app, settings, calls, started = learning_api
    response = client.post("/api/v1/jobs", json={"kind": "factor_training", "params": {"wait_for_cancel": True}})
    identity = response.json()["id"]
    assert started.wait(3)
    assert client.post(f"/api/v1/jobs/{identity}/cancel").status_code == 200
    job = terminal(client, identity)
    assert job["status"] == "cancelled"
    assert job["cancel_effective"] is True
    assert app.state.research.calls == []


def test_learning_cli_preserves_explicit_symbols_and_fold_parameters(learning_api, monkeypatch, capsys):
    from lake_equity.cli import main

    client, app, settings, calls, started = learning_api
    monkeypatch.setattr(sys, "argv", ["lake-equity", "--root", str(settings.root), "learn",
                                     "--symbols", "600519.sh,000001.sz", "--start", "2024-01-01", "--end", "2026-10-22",
                                     "--groups", "price_volume,margin", "--backend", "qlib", "--train-sessions", "252",
                                     "--valid-sessions", "63", "--test-sessions", "21", "--max-folds", "2"])
    main()
    spec = calls[0][1]
    assert spec["symbols"] == ["600519.SH", "000001.SZ"]
    assert spec["factor_groups"] == ["price_volume", "margin"]
    assert spec["backend"] == "qlib"
    assert spec["test_sessions"] == 21
    assert spec["max_folds"] == 2
    assert "learn-synthetic" in capsys.readouterr().out


def test_learning_report_shows_coverage_comparisons_and_qualification():
    from test_frontend_contract import run_module

    run_module(r"""
import assert from 'node:assert/strict';
import {learningReport} from './pages/models.js';
const report={run_id:'learn-<unsafe>',status:'completed',spec:{symbols:['600519.SH'],start:'2024-01-01',end:'2026-10-22',backend:'local'},qualification:{strict_history_eligible:false,paper_allocation_eligible:false},dataset:{rows:30,symbols_requested:['600519.SH'],symbols_with_bars:1,valid_bar_rows:29,feature_eligible_rows:20,label_formula:'next_open_to_future_open',group_coverage:{margin:{nonempty_rows:20,complete_rows:18,row_coverage:.6,reason:'lagged'}}},sources:{read_ref:'fixed-reference',datasets:{margin:{revision_id:'revision-123',files:[{path:'one.parquet'}]}}},models:[{name:'legacy_ma_ridge',feature_count:20,metrics:{rank_ic:0,ic:null,rmse:.02,observations:10,dates:5},portfolio:{metrics:{total_return:0,max_drawdown:-.01}}},{name:'factor_lightgbm',feature_count:43,metrics:{rank_ic:.1},portfolio:{metrics:{total_return:.02}}}],feature_groups:{margin:['margin_balance']},folds:[],limitations:['<unverified>']};
report.benchmark={metrics:{total_return:.035,fills:8,rejected_orders:2,fees:12.5}};
report.models.push({name:'price_volume_ridge',feature_count:38,metrics:{},portfolio:{metrics:{}},excess_return_vs_equal_weight:-.0125},{name:'price_volume_lightgbm',feature_count:38,metrics:{},portfolio:{metrics:{}},excess_return_vs_equal_weight:0},{name:'factor_ridge',feature_count:43,metrics:{},portfolio:{metrics:{}}});
const html=learningReport(report);
assert(html.includes('均线特征 · Ridge 基线'));
assert(html.includes('多因子 · LightGBM'));
assert(html.includes('纯量价 38 项 · Ridge'));
assert(html.includes('纯量价 38 项 · LightGBM'));
assert(html.includes('同样本等权基准'));
assert(html.includes('3.5%'));
assert(html.includes('-1.25 个百分点'));
assert(html.includes('0 个百分点'));
assert(html.includes('非指数超额'));
assert(html.includes('60%'));
assert(html.includes('revision-123'));
assert(html.includes('fixed-reference'));
assert(html.includes('next_open_to_future_open'));
assert(html.includes('严格历史资格 未通过'));
assert(html.includes('>0%<'));
assert(html.includes('&lt;unsafe&gt;'));
assert(html.includes('&lt;unverified&gt;'));
assert(!html.includes('<unverified>'));
""")


def test_learning_api_client_reads_without_starting_training():
    from test_frontend_contract import run_module

    run_module(r"""
import assert from 'node:assert/strict';
import {api} from './core/api.js';
const calls=[];
globalThis.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.learningCatalog();await api.learningRuns();await api.learningRun('learn-one');
assert.deepEqual(calls.map(call=>call.url),['/api/v1/learning/catalog','/api/v1/learning/runs','/api/v1/learning/runs/learn-one']);
assert(calls.every(call=>!call.options.method || call.options.method==='GET'));
""")
