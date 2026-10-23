from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from filelock import Timeout

from lake_equity.market.routes import create_market_router


@pytest.fixture
def market_client(tmp_path, monkeypatch):
    from lake_equity.market import engine, specialties, store

    class FakeStore:
        def read_snapshot(self, read_ref):
            return {"read_ref": read_ref, "datasets": {}}

    class FakeService:
        def __init__(self, market_store):
            self.store = market_store
            self.calls = []
            self.error = None

        def record(self, method, *args, **kwargs):
            if self.error:
                raise self.error
            self.calls.append((method, args, kwargs))
            return {"view_id": "fixed-view", "read_ref": "fixed-read"}

        def status(self):
            return self.record("status")

        def prepare(self, **kwargs):
            return self.record("prepare", **kwargs)

        def capital_view(self, *args, **kwargs):
            return self.record("capital_view", *args, **kwargs)

        def history(self, *args, **kwargs):
            return self.record("history", *args, **kwargs)

        def members(self, *args, **kwargs):
            return self.record("members", *args, **kwargs)

    service = FakeService(FakeStore())
    monkeypatch.setattr(store, "MarketStore", lambda root: service.store)
    monkeypatch.setattr(engine, "MarketFeaturesService", lambda market_store: service)
    monkeypatch.setattr(specialties, "specialty_view", lambda market_store, snapshot, section: {
        "read_ref": snapshot["read_ref"], "section": section, "panels": [],
    })
    app = FastAPI()
    app.include_router(create_market_router(tmp_path))
    with TestClient(app) as client:
        yield client, service


def test_market_reads_never_prepare_and_require_a_view(market_client):
    client, service = market_client
    assert client.get("/api/v1/market/status").json()["latest_view"]["view_id"] == "fixed-view"
    assert client.get("/api/v1/market/features").status_code == 422
    assert client.get("/api/v1/market/features?view_id=fixed-view&kind=industry&sort_by=activity&limit=30").status_code == 200
    assert [call[0] for call in service.calls] == ["status", "capital_view"]
    assert service.calls[-1][1] == ("fixed-view",)


def test_market_prepare_validates_explicit_scope(market_client):
    client, service = market_client
    response = client.post("/api/v1/market/prepare", json={"basis": "source", "end": "2026-10-21"})
    assert response.status_code == 200
    assert service.calls[-1] == ("prepare", (), {"end": "2026-10-21", "basis": "source"})
    assert client.post("/api/v1/market/prepare", json={"basis": "invented"}).status_code == 422
    assert client.post("/api/v1/market/prepare", json={"symbols": ["600000.SH"]}).status_code == 422
    assert client.post("/api/v1/market/prepare", json={"end": "not-a-date"}).status_code == 422


def test_market_detail_keeps_board_identity_and_real_window(market_client):
    client, service = market_client
    response = client.get("/api/v1/market/boards/em:industry:123/history?view_id=fixed-view&window=60")
    assert response.status_code == 200
    assert service.calls[-1] == ("history", ("fixed-view", "em:industry:123"), {"window": 60})
    assert client.get("/api/v1/market/boards/em:123/history?view_id=fixed-view&window=50").status_code == 422
    response = client.get("/api/v1/market/boards/em:123/members?view_id=fixed-view&offset=100&limit=50")
    assert response.status_code == 200
    assert service.calls[-1] == ("members", ("fixed-view", "em:123"), {"offset": 100, "limit": 50})
    assert client.get("/api/v1/market/boards/em:123/members?view_id=fixed-view&offset=-1").status_code == 422


def test_market_specialty_reuses_the_board_snapshot(market_client):
    client, service = market_client
    response = client.get("/api/v1/market/specialties?view_id=fixed-view&section=industry")
    assert response.status_code == 200
    assert response.json() == {"read_ref": "fixed-read", "section": "industry", "panels": []}
    assert service.calls == [("capital_view", ("fixed-view",), {"limit": 1})]
    assert client.get("/api/v1/market/specialties?view_id=fixed-view&section=unknown").status_code == 422


@pytest.mark.parametrize("error,status", [(ValueError("invalid view"), 400), (FileNotFoundError(), 404), (Timeout("market.lock"), 409)])
def test_market_errors_have_actionable_status(market_client, error, status):
    client, service = market_client
    service.error = error
    assert client.get("/api/v1/market/status").status_code == status


def test_market_preview_never_starts_or_reconciles_main_jobs(tmp_path, monkeypatch):
    from lake_equity import jobs

    preview_path = Path(__file__).resolve().parents[1] / "scripts" / "preview_market.py"
    specification = importlib.util.spec_from_file_location("market_preview_test", preview_path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("preview-only", encoding="utf-8")
    (tmp_path / "frontend" / "app.js").write_text("export const preview = true;", encoding="utf-8")
    original_job = tmp_path / "var" / "jobs" / "running.json"
    original_job.parent.mkdir(parents=True)
    original_job.write_bytes(b'{"status":"running"}')

    def forbidden_manager(*args, **kwargs):
        raise AssertionError("Preview must not instantiate JobManager")

    monkeypatch.setattr(jobs, "JobManager", forbidden_manager)
    app = module.create_preview(tmp_path)
    with TestClient(app) as client:
        assert client.get("/").text == "preview-only"
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/app.js").text == "export const preview = true;"
        assert client.post("/api/v1/jobs", json={"kind": "scan"}).status_code == 405
        assert client.post("/api/v1/market/prepare", json={}, headers={"Origin": "http://external.invalid"}).status_code == 403
    assert original_job.read_bytes() == b'{"status":"running"}'
    assert not (tmp_path / "var" / "research_inputs").exists()


def test_market_preview_proxies_only_get_to_the_fixed_primary(tmp_path, monkeypatch):
    preview_path = Path(__file__).resolve().parents[1] / "scripts" / "preview_market.py"
    specification = importlib.util.spec_from_file_location("market_preview_proxy_test", preview_path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    (tmp_path / "frontend").mkdir()
    requests = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 30, "trust_env": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params):
            requests.append((url, str(params)))
            return httpx.Response(200, json={"status": "running"})

    app = module.create_preview(tmp_path, primary_port=8130)
    with TestClient(app) as client:
        monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
        response = client.get("/api/v1/jobs/existing?limit=1")
        assert response.status_code == 200
        assert response.json() == {"status": "running"}
        assert client.post("/api/v1/jobs", json={}).status_code == 405
    assert requests == [("http://127.0.0.1:8130/api/v1/jobs/existing", "limit=1")]
