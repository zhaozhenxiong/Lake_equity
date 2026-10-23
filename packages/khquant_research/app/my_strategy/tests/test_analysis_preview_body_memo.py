"""Analysis preview rendered-body memo and _analysis_response semantics."""
import gzip
import json

import pytest
from fastapi.responses import JSONResponse
from starlette.responses import Response

from my_strategy.web_dashboard import api


@pytest.fixture(autouse=True)
def _clear_memo():
    api._PREVIEW_BODY_MEMO.clear()
    yield
    api._PREVIEW_BODY_MEMO.clear()


def test_analysis_response_plain_dict_uses_jsonresponse():
    response = api._analysis_response({"ok": True})
    assert isinstance(response, JSONResponse)
    assert json.loads(response.body) == {"ok": True}


def test_fresh_preview_is_served_but_not_memoized():
    viewed = {"preview_id": "p1", "reused": False, "x": 1}
    response = api._analysis_response(viewed)
    assert isinstance(response, Response)
    assert json.loads(response.body)["reused"] is False
    assert "p1" not in api._PREVIEW_BODY_MEMO


def test_store_hit_is_memoized_and_repeat_views_serve_same_bytes():
    viewed = {"preview_id": "p2", "reused": True, "x": 2}
    first = api._analysis_response(viewed)
    second = api._analysis_response(viewed)
    assert first.body == second.body == JSONResponse(viewed).body
    assert json.loads(second.body)["reused"] is True


def test_preview_body_capacity_evicts_oldest(monkeypatch):
    monkeypatch.setenv("KHQUANT_ANALYSIS_PREVIEW_BODY_ENTRIES", "1")
    api._preview_body_put("a", b"1")
    api._preview_body_put("b", b"2")
    assert api._preview_body_get("a") is None
    assert api._preview_body_get("b") == b"2"


def test_preview_body_layer_disabled(monkeypatch):
    monkeypatch.setenv("KHQUANT_ANALYSIS_PREVIEW_BODY_ENTRIES", "0")
    api._preview_body_put("a", b"1")
    assert not api._PREVIEW_BODY_MEMO
    viewed = {"preview_id": "p3", "reused": True}
    first = api._analysis_response(viewed)
    assert not api._PREVIEW_BODY_MEMO
    assert json.loads(first.body) == viewed


def test_preview_body_roundtrip_is_gzipped():
    api._preview_body_put("k", b'{"a":1}')
    assert api._PREVIEW_BODY_MEMO["k"] == gzip.compress(b'{"a":1}', compresslevel=1)
    assert api._preview_body_get("k") == b'{"a":1}'


# --- Round 11: async preview store write -------------------------------------


def test_enqueue_preview_write_lands_and_roundtrips(tmp_path):
    from my_strategy.storage.research_store import ResearchStore
    store = ResearchStore(root=tmp_path / "store")
    try:
        identity = {"kind": "probe", "n": 1}
        result = {"rows": list(range(500)), "text": "y" * 2000}
        future = api._enqueue_preview_write(store, identity, result)
        assert future.result(timeout=30) is None
        assert store.get(identity, namespace="analysis_previews") == result
    finally:
        store.close()


def test_enqueue_preview_write_orders_gc_before_put(tmp_path):
    from my_strategy.storage.research_store import ResearchStore
    store = ResearchStore(root=tmp_path / "store")
    calls = []
    original_gc, original_put = store.gc, store.put
    store.gc = lambda *a, **k: (calls.append("gc"), original_gc(*a, **k))[1]
    store.put = lambda *a, **k: (calls.append("put"), original_put(*a, **k))[1]
    try:
        api._enqueue_preview_write(store, {"kind": "probe"}, {"v": 1}).result(timeout=30)
        assert calls == ["gc", "put"]
    finally:
        store.close()


def test_enqueue_preview_write_failure_is_logged_not_raised(tmp_path, caplog):
    from my_strategy.storage.research_store import ResearchStore
    store = ResearchStore(root=tmp_path / "store")
    store.put = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    try:
        with caplog.at_level("ERROR", logger=api.__name__):
            assert api._enqueue_preview_write(store, {"kind": "probe"}, {"v": 1}).result(timeout=30) is None
        assert any("preview write failed" in record.getMessage() for record in caplog.records)
    finally:
        store.close()


def test_drain_preview_writes_waits_for_queue(tmp_path):
    from my_strategy.storage.research_store import ResearchStore
    store = ResearchStore(root=tmp_path / "store")
    try:
        api._enqueue_preview_write(store, {"kind": "a"}, {"v": 1})
        api._enqueue_preview_write(store, {"kind": "b"}, {"v": 2})
        api._drain_preview_writes()
        assert store.get({"kind": "a"}, namespace="analysis_previews") == {"v": 1}
        assert store.get({"kind": "b"}, namespace="analysis_previews") == {"v": 2}
    finally:
        store.close()


# --- Round 11: memo-hot fast path through the analysis route ------------------


@pytest.fixture
def manager(tmp_path):
    from my_strategy.storage.czsc_results import ResultStore
    from my_strategy.web_dashboard.tasks import TaskManager
    store = ResultStore(tmp_path / "runs.db", tmp_path / "runs")
    manager = TaskManager(tmp_path / "jobs.db", store, workers=1)
    yield manager
    manager.close()


def _patch_analysis(monkeypatch, manager, calls):
    from my_strategy.web_dashboard import trend_strategy
    monkeypatch.setattr(api, "_research_preview_identity", lambda spec, settings:
        {"fixture": str(manager.results.db_path),
         "request": spec.model_dump(mode="json", exclude_none=True), "input_sha": "fixed-input"})
    def calculate(**kwargs):
        calls.append(kwargs)
        return {"symbol": kwargs["symbol"], "data_end": "2025-09-30", "data_version": "fixed-input",
                "as_of": kwargs.get("as_of"), "frequencies": {}, "events": [],
                "research": {"model_run_id": "m1"}}
    monkeypatch.setattr(trend_strategy, "analyze_trend_consensus", calculate)


_BODY = {"research": True, "symbol": "000001.SZ", "start": "2025-01-01", "as_of": "2025-09-30",
         "analysis_method": "legacy_reference", "analysis_mode": "reference",
         "consensus_version": "reference_position_v1", "usage_mode": "historical",
         "model_policy": "auto", "device": "auto", "current_position_fraction": 0}


def test_memo_hot_hit_serves_without_store_get(manager, monkeypatch):
    from my_strategy.web_dashboard.app import create_app
    from fastapi.testclient import TestClient
    calls = []
    _patch_analysis(monkeypatch, manager, calls)
    with TestClient(create_app(manager)) as client:
        store = client.app.state.light.store
        first = client.post("/api/analysis", json=_BODY)
        assert first.status_code == 200 and first.json()["reused"] is False
        api._drain_preview_writes()
        second = client.post("/api/analysis", json=_BODY)
        assert second.json()["reused"] is True  # store hit; fills the body memo
        assert api._PREVIEW_BODY_MEMO, "memo must be filled from the real store hit"
        monkeypatch.setattr(store, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("store.get called")))
        third = client.post("/api/analysis", json=_BODY)
        assert third.status_code == 200
        assert third.content == second.content  # memo body, byte-exact
        assert third.json()["reused"] is True
        assert len(calls) == 1


def test_verify_miss_drops_memo_and_falls_back_to_full_read(manager, monkeypatch):
    from my_strategy.web_dashboard.app import create_app
    from fastapi.testclient import TestClient
    calls = []
    _patch_analysis(monkeypatch, manager, calls)
    with TestClient(create_app(manager)) as client:
        store = client.app.state.light.store
        client.post("/api/analysis", json=_BODY)
        api._drain_preview_writes()
        client.post("/api/analysis", json=_BODY)  # fills memo
        assert api._PREVIEW_BODY_MEMO
        # Simulate GC eviction of the store object: verify reports the miss,
        # the memo entry is dropped, and the request recomputes cleanly.
        key = next(iter(api._PREVIEW_BODY_MEMO))
        row = store.connection.execute("SELECT cache_key, sha256 FROM results").fetchone()
        path = store._path(row["sha256"])
        path.chmod(path.stat().st_mode | 0o200)  # stored objects are read-only
        path.unlink()
        store.connection.execute("DELETE FROM results WHERE cache_key=?", (row["cache_key"],))
        store.connection.execute("DELETE FROM objects WHERE sha256=?", (row["sha256"],))
        store.connection.commit()
        response = client.post("/api/analysis", json=_BODY)
        assert response.status_code == 200 and response.json()["reused"] is False
        assert len(calls) == 2  # recomputed after the memo could not be verified
        assert key not in api._PREVIEW_BODY_MEMO  # dropped; fresh responses never fill it
        api._drain_preview_writes()
