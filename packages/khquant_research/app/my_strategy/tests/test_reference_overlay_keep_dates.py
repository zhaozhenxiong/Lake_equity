"""Scan-mode keep_dates: skipped days never materialize, kept output identical.

Round 12 moved the scan's parent-side per-stock cost down by (a) trimming the
worker transport to the consumed pack fields and (b) assembling full per-day
payloads only for the market-history window days. The sequential reference
path still advances through every day from identical inputs, so kept rows,
latest_decision, status counts and every scan-visible field must match the
full assembly byte for byte.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from my_strategy.services import reference_runtime


@pytest.fixture
def overlay_inputs(monkeypatch, tmp_path):
    from my_strategy.services import reference_model, reference_training
    dates = pd.bdate_range("2025-01-01", periods=80).strftime("%Y-%m-%d").tolist()
    frame = pd.DataFrame({"symbol": "000001.SZ", "date": dates, "close": 10., "open": 10.,
        "high": 10.2, "low": 9.8, "source": "tushare", "has_trade_price": 1,
        "volume": 1e7, "amount": 1e8, "model_input_eligible": True,
        "liquidity_pool_eligible": True, "available_at": [day + "T15:00:00+08:00" for day in dates],
        "liquidity_observed_at": [day + "T15:00:00+08:00" for day in dates]})
    frame.loc[50, ["high", "low"]] = 9.0, 10.5  # invalid geometry exercises the marker path
    for column in reference_model.MA_TREND_COLUMNS:
        frame[column] = .5
    evaluations = [{"date": day, "available_at": day + "T15:00:00+08:00",
        **{source: {"date": day, "available_at": day + "T15:00:00+08:00", "input_eligible": True,
            "current_state": {"buy_score": .6, "sell_score": .2, "event_demand_support": .1,
                "event_supply_support": .05}, "reason_codes": []}
           for source in ("structure", "wyckoff", "ict")}}
        for day in dates]
    pack = {"frame": frame.copy(), "evaluations": evaluations, "events": [],
        "wyckoff_events": [{"event_id": "w1", "date": dates[70]}],
        "ict_events": [{"event_id": "i1", "date": dates[71]}],
        "incremental_state": {"resume": "heavy"}, "auxiliary_evaluations": [{"heavy": True}]}
    # Resume-machinery snapshot columns ride along in the real pack frame; the
    # scan transport drops them and the overlay must be indifferent.
    pack["frame"]["native_ict_snapshot"] = [{"heavy": day} for day in dates]
    manifest = tmp_path / "manifest.json"
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"keep-dates test model weights")
    model_manifest = {"schema_hash": reference_model.SCHEMA_HASH,
        "weights_sha256": hashlib.sha256(weights.read_bytes()).hexdigest(), "weights_file": weights.name}
    manifest.write_text(json.dumps(model_manifest))
    report = {"config": {"position": {}}, "folds": [{"name": "fold", "split": {
        "test_start": dates[40], "test_end": dates[-1]}, "model_path": str(manifest),
        "research_reconstruction": True, "predictive_gate": {"passed": True}}]}
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "reference_training.json").write_text(json.dumps(report))
    monkeypatch.setattr(reference_training, "active_reference_run_id", lambda: "reference-run")
    monkeypatch.setattr(reference_training, "load_reference_run", lambda run_id=None: (tmp_path, report))

    class Model:
        manifest = model_manifest
        def predict(self, values):
            return np.tile([.7, .1, .2], (len(values), 1))

    monkeypatch.setattr(reference_runtime, "_model_session", lambda *args: Model())
    return frame, dates, pack


def _overlay(frame, dates, pack, **kwargs):
    return reference_runtime.reference_overlay(frame, dates, device="cpu", prepared_native=pack, **kwargs)


def test_scan_mode_kept_rows_latest_counts_and_hashes_are_identical(overlay_inputs):
    frame, dates, pack = overlay_inputs
    window = dates[-30:]
    full = _overlay(frame, dates, pack)
    scan = _overlay(frame, dates, pack, keep_dates=window)
    assert scan["latest_decision"] == full["latest_decision"]
    assert {row["date"] for row in scan["rows"]} == set(window)
    full_rows = {row["date"]: row for row in full["rows"]}
    for row in scan["rows"]:
        assert row == full_rows[row["date"]]
    assert scan["compute_info"]["source_reason_counts"] == full["compute_info"]["source_reason_counts"]
    assert scan["trend_consensus"]["config"] == full["trend_consensus"]["config"]
    assert scan["reference_context_hash"] == full["reference_context_hash"]
    assert scan["position_reference"] == full["position_reference"]
    assert scan["reference_risk_plan"] == full["reference_risk_plan"]
    # Action-day points are built for kept days with unchanged identities.
    full_points = {point["date"]: point for point in full["reference_points"]}
    assert scan["reference_points"]
    assert all(point == full_points[point["date"]] for point in scan["reference_points"])
    assert {row["date"] for row in scan["rows"] if "event_id" in row} == \
        {point["date"] for point in scan["reference_points"]}


def test_scan_mode_missing_cutoff_session_still_matches_full_mode(overlay_inputs):
    frame, dates, pack = overlay_inputs
    trimmed_frame = frame.iloc[:-1]
    trimmed_pack = {**pack, "frame": pack["frame"].iloc[:-1].copy(),
        "evaluations": [entry for entry in pack["evaluations"] if entry["date"] != dates[-1]]}
    window = dates[50:78]  # neither the cutoff nor the last available day is in the window
    full = _overlay(trimmed_frame, dates, trimmed_pack, as_of=dates[-1])
    scan = _overlay(trimmed_frame, dates, trimmed_pack, as_of=dates[-1], keep_dates=window)
    assert scan["latest_decision"] == full["latest_decision"]
    assert scan["latest_decision"]["direction"] == "UNAVAILABLE"
    assert "missing_target_market_session" in scan["latest_decision"]["reason_codes"]
    # The last available day is always kept because rows[-1] anchors latest_decision.
    assert {row["date"] for row in scan["rows"]} == set(window) | {dates[-2]}
    full_rows = {row["date"]: row for row in full["rows"]}
    for row in scan["rows"]:
        assert row == full_rows[row["date"]]


def test_scan_mode_with_window_trimmed_pack_matches_full_mode(overlay_inputs):
    """The production pairing: worker-trimmed pack + keep_dates overlay."""
    frame, dates, pack = overlay_inputs
    window = dates[-30:]
    keep_days = set(window)
    lite_frame = pack["frame"].drop(columns=["native_ict_snapshot"])
    lite_frame.attrs = dict(pack["frame"].attrs)
    lite = {"frame": lite_frame, "events": pack["events"],
        "wyckoff_events": pack["wyckoff_events"], "ict_events": pack["ict_events"],
        "evaluations": [entry for entry in pack["evaluations"] if entry["date"] in keep_days]}
    full = _overlay(frame, dates, pack)
    scan = _overlay(frame, dates, lite, keep_dates=window)
    assert scan["latest_decision"] == full["latest_decision"]
    full_rows = {row["date"]: row for row in full["rows"]}
    assert all(row == full_rows[row["date"]] for row in scan["rows"])
    assert scan["native_pack"]["evaluations"] == lite["evaluations"]
    assert scan["wyckoff_events"] == pack["wyckoff_events"]
    assert scan["ict_events"] == pack["ict_events"]


def test_full_mode_is_unchanged_when_keep_dates_is_none(overlay_inputs):
    frame, dates, pack = overlay_inputs
    out = _overlay(frame, dates, pack)
    assert [row["date"] for row in out["rows"]] == dates
    assert [row["date"] for row in out["trend_consensus"]["rows"]] == dates


def test_stock_worker_prepare_only_trims_transport_to_window(monkeypatch):
    from my_strategy.services import native_pack_cache
    from my_strategy.web_dashboard import trend_strategy
    days = ["2026-09-24", "2026-09-25", "2026-09-28"]
    features = pd.DataFrame({"date": days, "close": [1., 1., 1.]})
    item = {"features": features, "data_version": "v1", "data_end": days[-1], "raw": "heavy-raw-frame"}
    monkeypatch.setattr(trend_strategy, "_source_vintage", lambda *a, **k: (item, "source-ctx", days[-1]))
    evaluations = [{"date": day, "marker": index} for index, day in enumerate(days)]
    pack_frame = features.copy()
    for heavy in trend_strategy._SCAN_TRANSPORT_DROP_COLUMNS:
        pack_frame[heavy] = [{"resume": "only"}] * len(days)
    pack_frame["native_structure_buy_support"] = .5  # numeric evidence column stays
    pack_frame.attrs["schema_hash"] = "schema-1"
    pack = {"frame": pack_frame, "evaluations": evaluations, "events": [1],
        "wyckoff_events": [2], "ict_events": [3], "incremental_state": {"large": True},
        "auxiliary_evaluations": [9]}
    monkeypatch.setattr(native_pack_cache, "get_or_build_native", lambda *a, **k: (pack, "disk"))
    _, out_item, _, out_pack = trend_strategy._stock_worker(
        ("000001.SZ", days[-1], days, {"_prepare_only": True, "_keep_dates": days[-2:]}, days[-1]))
    assert set(out_item) == {"features", "data_version", "data_end"}
    assert out_item["features"] is features and out_item["data_version"] == "v1"
    assert set(out_pack) == {"frame", "events", "wyckoff_events", "ict_events", "evaluations"}
    assert [entry["date"] for entry in out_pack["evaluations"]] == days[-2:]
    assert out_pack["events"] == [1] and out_pack["wyckoff_events"] == [2] and out_pack["ict_events"] == [3]
    out_frame = out_pack["frame"]
    assert not set(trend_strategy._SCAN_TRANSPORT_DROP_COLUMNS) & set(out_frame.columns)
    assert out_frame["native_structure_buy_support"].tolist() == [.5] * len(days)
    assert out_frame.attrs["schema_hash"] == "schema-1"
    assert pack_frame.columns.tolist() != out_frame.columns.tolist()  # on-disk pack untouched
    # Without keep dates the pack passes through whole; the item stays trimmed.
    _, _, _, out_pack = trend_strategy._stock_worker(
        ("000001.SZ", days[-1], days, {"_prepare_only": True}, days[-1]))
    assert out_pack is pack


def test_batch_threads_keep_dates_into_payload_and_overlay(monkeypatch):
    from my_strategy.adapters import czsc_adapter
    from my_strategy.services import czsc_trend_runtime
    from my_strategy.web_dashboard import trend_strategy
    monkeypatch.setattr(czsc_adapter, "latest_market_date", lambda: "2026-09-29")
    seen_payloads, seen_overlay = [], {}

    def prepare(payload):
        seen_payloads.append(payload)
        return payload[0], {"features": "f", "data_version": "v", "data_end": "d"}, "ctx", {}

    def overlay(features, dates, **kwargs):
        seen_overlay.update(kwargs)
        return {"latest_decision": {}}

    monkeypatch.setattr(trend_strategy, "_stock_worker", prepare)
    monkeypatch.setattr(czsc_trend_runtime, "reference_overlay", overlay)
    list(trend_strategy._batch(["a"], "2026-09-29", ["2026-09-29"], {"analysis_mode": "reference"}, 1,
        keep_dates=["2026-09-29"]))
    assert seen_payloads[0][3]["_keep_dates"] == ["2026-09-29"]
    assert seen_overlay["keep_dates"] == ["2026-09-29"]
    seen_payloads.clear()
    seen_overlay.clear()
    list(trend_strategy._batch(["a"], "2026-09-29", ["2026-09-29"], {"analysis_mode": "reference"}, 1))
    assert seen_payloads[0][3]["_keep_dates"] is None and seen_overlay["keep_dates"] is None
