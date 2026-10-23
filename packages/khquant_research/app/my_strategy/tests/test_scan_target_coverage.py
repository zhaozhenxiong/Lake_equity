"""Scan target date must follow whole-universe coverage, not MAX(date).

Regression coverage for the 2026-10-21 incident: a single-stock update lifted
MAX(date) to a day the rest of the market did not have, and the full-market
scan then failed every other stock with "行情缺少扫描日".
"""
from __future__ import annotations

from types import SimpleNamespace
import sqlite3
import threading
import time

import pandas as pd
import pytest

from my_strategy.adapters import czsc_adapter
from my_strategy.adapters.czsc_adapter import covered_market_date


def _raw_db(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE stock_daily_normalized(stock TEXT,date TEXT,close REAL,"
                     "source TEXT,has_trade_price INTEGER,PRIMARY KEY(stock,date))")
        conn.executemany("INSERT INTO stock_daily_normalized VALUES(?,?,?,?,?)", rows)
    return path


def _rows(symbols, dates, source="sina_unadjusted_v1", has_trade_price=1):
    return [[symbol, day, 10.0, source, has_trade_price] for symbol in symbols for day in dates]


DATES = [f"2026-10-{day:02d}" for day in range(5, 17) if day not in (10, 11)]  # 10 trading days


def test_uniform_coverage_keeps_latest_date(tmp_path):
    db = _raw_db(tmp_path / "raw.db", _rows(["000001.SZ", "000002.SZ"], DATES))
    info = covered_market_date(DATES[-1], db_path=db)
    assert info["date"] == DATES[-1] and info["clamped"] is False
    assert info["symbols"] == info["reference_symbols"] == info["requested_symbols"] == 2
    assert info["coverage_check"] == "ok"


def test_single_stock_bump_clamps_to_last_covered_day(tmp_path):
    rows = _rows(["000001.SZ", "000002.SZ"], DATES)
    rows += _rows(["000001.SZ"], ["2026-10-17", "2026-10-19"])
    db = _raw_db(tmp_path / "raw.db", rows)
    info = covered_market_date("2026-10-19", db_path=db)
    assert info["date"] == DATES[-1]
    assert info["requested_latest"] == "2026-10-19"
    assert info["requested_symbols"] == 1
    assert info["reference_symbols"] == 2
    assert info["clamped"] is True


def test_threshold_boundary_admits_ninety_percent_day(tmp_path):
    symbols = [f"00000{index}.SZ" for index in range(10)]
    rows = _rows(symbols, DATES) + _rows(symbols[:9], ["2026-10-17"])
    db = _raw_db(tmp_path / "raw.db", rows)
    info = covered_market_date("2026-10-17", db_path=db)
    assert info["date"] == "2026-10-17" and info["clamped"] is False


def test_partial_day_below_threshold_falls_back(tmp_path):
    symbols = [f"00000{index}.SZ" for index in range(10)]
    rows = _rows(symbols, DATES) + _rows(symbols[:8], ["2026-10-17"])
    db = _raw_db(tmp_path / "raw.db", rows)
    info = covered_market_date("2026-10-17", db_path=db)
    assert info["date"] == DATES[-1] and info["clamped"] is True


def test_end_bound_is_respected(tmp_path):
    rows = _rows(["000001.SZ"], DATES + ["2026-10-17"])
    db = _raw_db(tmp_path / "raw.db", rows)
    info = covered_market_date(DATES[-1], db_path=db)
    assert info["date"] == DATES[-1]


def test_missing_database_degrades_to_legacy_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(czsc_adapter, "latest_market_date", lambda end=None, **_kw: "2026-10-16")
    info = covered_market_date("2026-10-16", db_path=tmp_path / "absent.db")
    assert info["date"] == "2026-10-16"
    assert info["coverage_check"] == "unavailable" and info["clamped"] is False


def test_empty_database_reports_no_date(tmp_path):
    db = _raw_db(tmp_path / "raw.db", [])
    assert covered_market_date("2026-10-16", db_path=db) is None


def test_board_qualified_date_survives_single_calc_symbol_bump(tmp_path):
    from my_strategy.services import board_fluent
    calc = ["000001.SZ", "000002.SZ", "600000.SH", "600010.SH", "300001.SZ"]
    rows = _rows(calc, DATES)
    rows += _rows(["000001.SZ"], ["2026-10-17"])
    rows += _rows(["920001.BJ"], ["2099-01-01"])  # outside the calculation scope entirely
    db = _raw_db(tmp_path / "raw.db", rows)
    assert board_fluent.latest_qualified_price_date(db) == DATES[-1]


def test_board_qualified_date_keeps_majority_covered_day(tmp_path):
    # Per-stock missing prices on the target day are a supported board outcome:
    # a majority-covered day stays the qualified date instead of falling back.
    from my_strategy.services import board_fluent
    calc = ["000001.SZ", "000002.SZ", "600000.SH", "600010.SH", "300001.SZ",
            "000063.SZ", "600036.SH"]
    rows = _rows(calc, DATES) + _rows(calc[:6], ["2026-10-17"])
    db = _raw_db(tmp_path / "raw.db", rows)
    assert board_fluent.latest_qualified_price_date(db) == "2026-10-17"


def test_scan_targets_covered_day_and_reports_clamp(tmp_path, monkeypatch):
    from my_strategy.services import czsc_market_history, czsc_trend_runtime, czsc_trend_support
    from my_strategy.storage import czsc_results
    from my_strategy.web_dashboard import trend_strategy

    bumped = "2026-10-19"
    symbols = ["000001.SZ", "000002.SZ"]
    rows = _rows(symbols, DATES) + _rows(["000001.SZ"], [bumped])
    monkeypatch.setenv("KHQUANT_RAW_DB", str(_raw_db(tmp_path / "raw.db", rows)))
    store = czsc_results.ResultStore(tmp_path / "results.db", tmp_path / "runs")
    context = SimpleNamespace(run_id="scan-clamp", to_dict=lambda: {"run_id": "scan-clamp"},
        subdir=lambda name: tmp_path / "runs/scan-clamp" / name)
    monkeypatch.setattr(czsc_adapter, "list_symbols", lambda: [{"symbol": s} for s in symbols])
    monkeypatch.setattr(czsc_trend_support, "_calendar", lambda _: (DATES + [bumped], "calendar"))
    monkeypatch.setattr(czsc_results, "ResultStore", lambda: store)
    monkeypatch.setattr(trend_strategy, "create_run_context", lambda **kwargs: context)
    monkeypatch.setattr(czsc_market_history, "load_classification",
        lambda selected: ({}, {"mode": "unavailable", "note": "fixture"}))

    def prepare(payload):
        symbol = payload[0]
        return symbol, {"features": pd.DataFrame({"date": [payload[1]]}), "data_version": "raw-v1",
            "data_end": payload[1]}, "source", {}

    def infer(features, *args, **kwargs):
        row = {"date": DATES[-1], "action": "WAIT", "direction": "UNAVAILABLE", "agents": {}}
        return {"latest_decision": row, "rows": [row], "features": features}

    monkeypatch.setattr(trend_strategy, "_stock_worker", prepare)
    monkeypatch.setattr(czsc_trend_runtime, "reference_overlay", infer)
    result = trend_strategy.scan_trend_consensus(symbols=symbols, end=bumped, structure_only=True,
        cpu_workers=1, progress=None, check_cancel=None)
    assert result["data_end"] == DATES[-1]
    assert result["data_range"] == {"start": "2026-01-01", "end": DATES[-1], "requested_end": bumped}
    coverage = result["data_coverage"]
    assert coverage["clamped"] is True and coverage["symbols"] == 2
    assert coverage["requested_latest"] == bumped and coverage["requested_symbols"] == 1
    assert DATES[-1] in result["coverage_note"] and bumped in result["coverage_note"]
    assert result["coverage"]["successful"] == 2 and result["coverage"]["failed"] == 0


def test_scan_threads_history_window_into_batch(tmp_path, monkeypatch):
    """The scan's keep-dates scan mode is fed by the market-history window."""
    from my_strategy.services import czsc_market_history, czsc_trend_runtime, czsc_trend_support
    from my_strategy.storage import czsc_results
    from my_strategy.web_dashboard import trend_strategy

    symbols = ["000001.SZ", "000002.SZ"]
    monkeypatch.setenv("KHQUANT_RAW_DB", str(_raw_db(tmp_path / "raw.db", _rows(symbols, DATES))))
    store = czsc_results.ResultStore(tmp_path / "results.db", tmp_path / "runs")
    context = SimpleNamespace(run_id="scan-keep-dates", to_dict=lambda: {"run_id": "scan-keep-dates"},
        subdir=lambda name: tmp_path / "runs/scan-keep-dates" / name)
    monkeypatch.setattr(czsc_adapter, "list_symbols", lambda: [{"symbol": s} for s in symbols])
    monkeypatch.setattr(czsc_trend_support, "_calendar", lambda _: (DATES, "calendar"))
    monkeypatch.setattr(czsc_results, "ResultStore", lambda: store)
    monkeypatch.setattr(trend_strategy, "create_run_context", lambda **kwargs: context)
    monkeypatch.setattr(czsc_market_history, "load_classification",
        lambda selected: ({}, {"mode": "unavailable", "note": "fixture"}))

    def prepare(payload):
        symbol = payload[0]
        assert payload[3]["_keep_dates"] == list(captured["keep_dates"])
        return symbol, {"features": pd.DataFrame({"date": [payload[1]]}), "data_version": "raw-v1",
            "data_end": payload[1]}, "source", {}

    def infer(features, *args, **kwargs):
        assert kwargs["keep_dates"] == list(captured["keep_dates"])
        row = {"date": DATES[-1], "action": "WAIT", "direction": "UNAVAILABLE", "agents": {}}
        return {"latest_decision": row, "rows": [row], "features": features}

    monkeypatch.setattr(trend_strategy, "_stock_worker", prepare)
    monkeypatch.setattr(czsc_trend_runtime, "reference_overlay", infer)
    captured = {}
    real_batch = trend_strategy._batch

    def spy(symbols_, end, dates, params, workers, **kwargs):
        captured.update(kwargs)
        return real_batch(symbols_, end, dates, params, workers, **kwargs)

    monkeypatch.setattr(trend_strategy, "_batch", spy)
    trend_strategy.scan_trend_consensus(symbols=symbols, end=DATES[-1], structure_only=True,
        cpu_workers=1, progress=None, check_cancel=None)
    window = list(captured["keep_dates"])
    assert window and window[-1] == DATES[-1] and len(window) <= 30
    assert all(day in DATES for day in window)


def test_task_manager_scan_records_coverage_note(tmp_path, monkeypatch):
    from my_strategy.services import czsc_market_history, czsc_trend_runtime, czsc_trend_support
    from my_strategy.storage import czsc_results
    from my_strategy.web_dashboard import trend_strategy
    from my_strategy.web_dashboard.tasks import TaskManager

    bumped = "2026-10-19"
    symbols = ["000001.SZ", "000002.SZ"]
    rows = _rows(symbols, DATES) + _rows(["000001.SZ"], [bumped])
    monkeypatch.setenv("KHQUANT_RAW_DB", str(_raw_db(tmp_path / "raw.db", rows)))
    store = czsc_results.ResultStore(tmp_path / "results.db", tmp_path / "runs")
    context = SimpleNamespace(run_id="scan-clamp-task", to_dict=lambda: {"run_id": "scan-clamp-task"},
        subdir=lambda name: tmp_path / "runs/scan-clamp-task" / name)
    monkeypatch.setattr(czsc_adapter, "list_symbols", lambda: [{"symbol": s} for s in symbols])
    monkeypatch.setattr(czsc_trend_support, "_calendar", lambda _: (DATES + [bumped], "calendar"))
    monkeypatch.setattr(czsc_results, "ResultStore", lambda: store)
    monkeypatch.setattr(trend_strategy, "create_run_context", lambda **kwargs: context)
    monkeypatch.setattr(czsc_market_history, "load_classification",
        lambda selected: ({}, {"mode": "unavailable", "note": "fixture"}))

    def prepare(payload):
        symbol = payload[0]
        return symbol, {"features": pd.DataFrame({"date": [payload[1]]}), "data_version": "raw-v1",
            "data_end": payload[1]}, "source", {}

    def infer(features, *args, **kwargs):
        row = {"date": DATES[-1], "action": "WAIT", "direction": "UNAVAILABLE", "agents": {}}
        return {"latest_decision": row, "rows": [row], "features": features}

    monkeypatch.setattr(trend_strategy, "_stock_worker", prepare)
    monkeypatch.setattr(czsc_trend_runtime, "reference_overlay", infer)
    manager = TaskManager(tmp_path / "tasks.db", store, workers=1)
    try:
        job = manager.submit("scan", {"symbols": symbols, "end": bumped, "research": False,
            "cpu_workers": 1, "link_boards": False})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            final = manager.get(job["job_id"])
            if final["status"] not in {"queued", "running", "cancelling"}:
                break
            threading.Event().wait(.01)
        assert final["status"] == "succeeded", final["error"]
        assert final["result"]["data_end"] == DATES[-1]
        assert final["result"]["data_coverage"]["clamped"] is True
        assert "coverage_note" in final["result"]
    finally:
        manager.close()
