import gzip
import hashlib
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from lake_equity.research import ResearchBridge


def make_bridge(root):
    return ResearchBridge(raw_db=root / "inputs" / "raw.db", research_root=root / "metadata" / "light_research",
                          results_db=root / "processed" / "czsc" / "results.db", runs_root=root / "runs")


def market_fixture(symbol="000001.SZ", count=60):
    dates = pd.bdate_range("2025-01-02", periods=count).strftime("%Y-%m-%d").tolist()
    prices = 10 + np.arange(count) * .01
    frame = pd.DataFrame({"symbol": symbol, "date": dates, "open": prices, "high": prices + .2,
                          "low": prices - .2, "close": prices, "volume": 10_000_000.,
                          "amount": prices * 10_000_000., "source": "sina_unadjusted_v1", "has_trade_price": 1})
    event = {"date": dates[25], "event_id": symbol + ":synthetic-confirmed-25", "confirmed": True, "side": "BUY",
             "available_at": dates[25] + "T15:00:00+08:00", "risk_boundary": float(prices[25]) * .95}
    return dates, frame, event


class FixtureEngine:
    def __init__(self, symbols=("000001.SZ",)):
        self.data = {symbol: market_fixture(symbol) for symbol in symbols}

    def calendar(self):
        return next(iter(self.data.values()))[0], {"kind": "isolated-synthetic-calendar"}

    def source(self, symbol, end, *, stop=None):
        if stop:
            stop.check()
        _, frame, event = self.data[symbol]
        return frame, frame, [event], {"symbol": symbol, "data_sha256": "synthetic-only"}


def test_feature_contract_keeps_all_twenty_columns_separate_from_raw_values(tmp_path, monkeypatch):
    from my_strategy.adapters import czsc_adapter
    from my_strategy.services.czsc_market_profiles import MA_TREND_COLUMNS

    bridge = make_bridge(tmp_path)
    bridge._light = FixtureEngine()
    dates, frame, event = bridge._light.data["000001.SZ"]
    frame.attrs["data_version"] = "synthetic-input-version"
    monkeypatch.setattr(czsc_adapter, "load_bars", lambda *args, **kwargs: frame.copy())
    result = bridge.features("000001.SZ", dates[-1], limit=1)
    assert result["feature_columns"] == list(MA_TREND_COLUMNS)
    assert len(result["feature_columns"]) == 20
    assert "open" in result["columns"] and "open" not in result["feature_columns"]
    assert all(column in result["rows"][0] for column in result["feature_columns"])


def test_empty_report_and_model_reads_do_not_create_storage(tmp_path):
    bridge = make_bridge(tmp_path / "unused")
    assert bridge.list_reports() == {"items": [], "count": 0}
    assert bridge.list_models() == {"items": [], "count": 0}
    with pytest.raises(KeyError):
        bridge.read_report("unknown")
    assert not (tmp_path / "unused").exists()


def test_structure_backtest_uses_real_broker_and_preserves_report_identity(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge._light = FixtureEngine()
    dates, frame, event = bridge._light.data["000001.SZ"]
    result = bridge.backtest({"symbols": ["000001.SZ"], "start": dates[25], "end": dates[-1],
                              "input_manifest_id": "synthetic-input-manifest", "initial_cash": 1_000_000,
                              "config": {"max_holding_sessions": 200, "time_exit_sessions": 200}})
    trade = result["trades"][0]
    assert trade["signal_date"] == event["date"]
    assert trade["date"] == dates[26]
    assert trade["shares"] % 100 == 0
    assert trade["ledger_version"] == 3
    assert trade["fee"] > 0
    assert result["profit_certification"] is False
    assert result["input_manifest_id"] == "synthetic-input-manifest"
    saved = bridge.read_report(result["lake_run_id"])
    assert saved["trades"] == result["trades"]
    assert saved["state"] == result["state"]
    listed = bridge.list_reports("backtest")["items"][0]
    assert listed["report_sha256"] == result["report_sha256"]
    manifest = json.loads((bridge.runs_root / result["lake_run_id"] / "reports" / "manifest.json").read_text())
    assert manifest["input_manifest_id"] == "synthetic-input-manifest"
    assert manifest["engine_identity_preserved"] is True
    path = bridge.runs_root / result["lake_run_id"] / "reports" / "lake_research.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        bridge.read_report(result["lake_run_id"])


def test_multi_stock_backtest_reconciles_shared_cash(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge._light = FixtureEngine(("000001.SZ", "600519.SH"))
    dates = bridge._light.calendar()[0]
    result = bridge.backtest({"symbols": ["000001.SZ", "600519.SH"], "start": dates[25], "end": dates[-1]})
    buys = [trade for trade in result["trades"] if trade["action"] == "BUY"]
    assert {trade["symbol"] for trade in buys} == {"000001.SZ", "600519.SH"}
    assert result["daily"][-1]["cash"] >= 0
    last = result["daily"][-1]
    assert last["equity"] == pytest.approx(last["cash"] + last["market_value"])


def test_historical_reference_cannot_silently_become_new_account(tmp_path):
    bridge = make_bridge(tmp_path)
    with pytest.raises(ValueError, match="Historical reference"):
        bridge.backtest({"analysis_method": "legacy_reference", "symbols": ["000001.SZ"],
                         "start": "2025-01-01", "end": "2025-02-01"})
    assert not bridge.results_db.exists()


def test_cancellation_prevents_backtest_publication(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge._light = FixtureEngine()
    dates = bridge._light.calendar()[0]
    with pytest.raises(InterruptedError):
        bridge.backtest({"symbols": ["000001.SZ"], "start": dates[5], "end": dates[-1]}, check_cancel=lambda: True)
    assert not bridge.results_db.exists()
    assert not bridge.runs_root.exists()


def test_report_reader_checks_path_boundary_without_initializing_engine(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.results_db.parent.mkdir(parents=True)
    with sqlite3.connect(bridge.results_db) as connection:
        connection.execute("CREATE TABLE czsc_runs(run_id TEXT,report_path TEXT)")
        connection.execute("INSERT INTO czsc_runs VALUES(?,?)", ("escape", "../secret.json"))
    with pytest.raises(ValueError, match="escaped"):
        bridge.read_report("escape")
    assert bridge._light is None
    with pytest.raises(ValueError):
        bridge.read_report("../escape")


def test_model_list_reads_verified_cas_without_touching_rows(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.research_root.mkdir(parents=True)
    artifact = {"model_type": "mlp", "status": "completed", "artifact": {
        "feature_profile": "ma_trend_v1", "variant": "A", "model_version": "frozen-identity"}}
    content = gzip.compress(json.dumps(artifact).encode(), mtime=0)
    digest = hashlib.sha256(content).hexdigest()
    path = bridge.research_root / "objects" / digest[:2] / (digest + ".json.gz")
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    database = bridge.research_root / "research.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE results(cache_key TEXT,namespace TEXT,sha256 TEXT,created_at REAL,accessed_at REAL)")
        connection.execute("INSERT INTO results VALUES(?,?,?,?,?)", ("a" * 64, "mlp_score_models", digest, 1., 2.))
    before = database.read_bytes()
    value = bridge.list_models()
    assert value["items"][0]["feature_profile"] == "ma_trend_v1"
    assert value["items"][0]["model_version"] == "frozen-identity"
    assert database.read_bytes() == before
    assert bridge._light is None
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="SHA256"):
        bridge.list_models()


def test_legacy_model_listing_preserves_frozen_identity(tmp_path):
    bridge = make_bridge(tmp_path)
    path = bridge.runs_root / "original-legacy-run" / "reports" / "reference_training.json"
    path.parent.mkdir(parents=True)
    report = {"run_id": "original-legacy-run", "family": "reference_position_v1", "status": "completed"}
    path.write_text(json.dumps(report), encoding="utf-8")
    before = path.read_bytes()
    result = bridge.list_models()["items"][0]
    assert result["model_id"] == "original-legacy-run"
    assert result["historical_only"] is True
    assert result["identity_upgraded"] is False
    assert bridge.read_report("original-legacy-run") == report
    assert path.read_bytes() == before


def test_methods_match_original_ordered_feature_contract(tmp_path):
    bridge = make_bridge(tmp_path)
    catalog = bridge.methods()
    profile = next(item for item in catalog["features"] if item["name"] == "ma_trend")
    assert len(profile["columns"]) == 20
    assert profile["columns"][0] == "ma5_bias"
    assert {method["id"] for method in catalog["methods"]} >= {"structure", "structure_kronos_ma_mlp", "structure_kronos_ma_l2"}
    assert not bridge.results_db.exists()


def test_preserved_native_engine_produces_independent_ict_and_volume_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("CZSC_HOME", str(tmp_path / "czsc-home"))
    from my_strategy.services.czsc_native_evidence import build_native_evidence

    dates, frame, _ = market_fixture(count=160)
    frame["date"] = pd.to_datetime(frame.date)
    frame.attrs.update(price_basis="unadjusted", volume_unit="shares", amount_unit="CNY",
                       verified_market_dates=dates)
    pack = build_native_evidence(frame, market_dates=dates)
    assert len(pack["frame"]) == len(frame)
    assert {"wyckoff_events", "ict_events", "events", "evaluations"} <= set(pack)
    assert {"native_ict_input_eligible", "native_w_input_eligible", "native_structure_input_eligible"} <= set(pack["frame"])
    assert len(pack["evaluations"]) == len(frame)
    assert pack["evaluations"][-1]["ict"]["available_at"].startswith(dates[-1])


def test_persistence_keeps_original_engine_run_and_model_identity(tmp_path):
    bridge = make_bridge(tmp_path)
    original = {"run_id": "frozen-engine-run", "model_id": "frozen-model", "identity": {"schema": "old-v1"}}
    result = bridge._persist("training", original, "manifest-v1")
    assert result["run_id"] == "frozen-engine-run"
    assert result["model_id"] == "frozen-model"
    assert result["lake_run_id"] != result["run_id"]
    assert original == {"run_id": "frozen-engine-run", "model_id": "frozen-model", "identity": {"schema": "old-v1"}}
    assert bridge.read_report(result["lake_run_id"])["identity"] == original["identity"]


def test_resume_scan_rejects_new_input_manifest_before_computation(tmp_path):
    bridge = make_bridge(tmp_path)
    previous = bridge._persist("scan", {"run_id": "original-scan", "rows": []}, "manifest-v1")
    with pytest.raises(ValueError, match="input manifest changed"):
        bridge.scan({"analysis_method": "structure_kronos", "resume_scan_id": previous["lake_run_id"],
                     "input_manifest_id": "manifest-v2"})
    assert bridge._light is None


def test_report_sort_compares_instants_and_treats_naive_legacy_dates_as_shanghai(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.results_db.parent.mkdir(parents=True)
    with sqlite3.connect(bridge.results_db) as connection:
        connection.execute("CREATE TABLE lake_research_runs(run_id TEXT,kind TEXT,created_at TEXT,report_sha256 TEXT,input_manifest_id TEXT,engine_run_id TEXT)")
        connection.execute("INSERT INTO lake_research_runs VALUES(?,?,?,?,?,?)",
                           ("latest-utc", "analysis", "2026-10-22T11:36:00+00:00", "digest", None, None))
        connection.execute("CREATE TABLE czsc_runs(run_id TEXT,kind TEXT,created_at TEXT,symbol TEXT,summary TEXT)")
        connection.execute("INSERT INTO czsc_runs VALUES(?,?,?,?,?)",
                           ("earlier-shanghai", "analysis", "2026-10-22T18:53:00+08:00", "000001.SZ", "{}"))
        connection.execute("INSERT INTO czsc_runs VALUES(?,?,?,?,?)",
                           ("naive-shanghai", "analysis", "2026-10-22T19:00:00", "000001.SZ", "{}"))
    before = bridge.results_db.read_bytes()
    assert [item["run_id"] for item in bridge.list_reports()["items"]] == ["latest-utc", "naive-shanghai", "earlier-shanghai"]
    assert bridge.results_db.read_bytes() == before
    assert bridge._light is None
