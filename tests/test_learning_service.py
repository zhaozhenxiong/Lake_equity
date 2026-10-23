"""Real local-model experiments over a synthetic, point-in-time dataset."""

import copy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from lake_equity.jobs import JobCancelled
from lake_equity.learning import dataset as dataset_module
from lake_equity.learning.service import LearningService


def synthetic_dataset():
    dates = pd.bdate_range("2025-01-02", periods=110).strftime("%Y-%m-%d").tolist()
    records = []
    for number, symbol in enumerate(("600000.SH", "000001.SZ", "600519.SH")):
        axis = np.arange(len(dates), dtype=float)
        opening = 10. + number + axis * .01 + .05 * np.sin(axis / 7 + number)
        close = opening + .015 * np.cos(axis / 5 + number)
        for index, day in enumerate(dates):
            mature = index + 5 < len(dates)
            records.append(dict(
                date=day, symbol=symbol, open=opening[index], close=close[index],
                high=opening[index] + .1, low=opening[index] - .1,
                volume=10_000_000., amount=opening[index] * 10_000_000.,
                label=close[index + 5] / opening[index + 1] - 1 if mature else np.nan,
                label_end=dates[index + 5] if mature else None,
                feature_eligible=not (index == 91 and number == 0),
                legacy_ma_eligible=not (index == 92 and number == 1),
                price_a=np.sin(index / 7 + number), price_b=np.cos(index / 11 + number),
                ma_a=np.sin(index / 10 + number), ma_b=(index % 13) / 13.,
                empty_in_train=np.nan if index < 80 else float(index),
            ))
    return dict(
        frame=pd.DataFrame(records), calendar=dates,
        feature_groups=dict(price_volume=["price_a", "price_b", "empty_in_train"],
                            legacy_ma=["ma_a", "ma_b"], margin=[], money_flow=[]),
        report=dict(rows=len(records), limitations=["Synthetic test data only"]),
        sources=dict(read_ref="a" * 64, datasets={"daily_bars": {"fixture": True}}),
    )


def request(data, **overrides):
    return dict(symbols=["600000.SH", "000001.SZ", "600519.SH"],
                start=data["calendar"][0], end=data["calendar"][-1],
                factor_groups=["price_volume"], horizon=5,
                train_sessions=40, valid_sessions=10, test_sessions=10, max_folds=2,
                models=["ridge", "lightgbm"], top_k=2, initial_cash=100_000.,
                **overrides)


def install_data(monkeypatch, data):
    monkeypatch.setattr(dataset_module, "build_dataset", lambda *args, **kwargs: copy.deepcopy(data))


@pytest.fixture
def completed(tmp_path, monkeypatch):
    data = synthetic_dataset()
    install_data(monkeypatch, data)
    service = LearningService(tmp_path)
    report = service.run(request(data))
    return service, report, data


def test_real_models_purge_labels_and_keep_unlabelled_inference(completed):
    service, report, data = completed
    destination = service.runs_root / report["run_id"]
    predictions = pd.read_parquet(destination / "predictions.parquet")
    assert {model["name"] for model in report["models"]} == {
        "legacy_ma_ridge", "factor_ridge", "factor_lightgbm"}
    assert report["qualification"]["strict_history_eligible"] is False
    assert report["qualification"]["paper_allocation_eligible"] is False
    for fold in report["folds"]:
        assert fold["purged_train_rows"] == 15
        assert fold["purged_valid_rows"] == 15
        assert fold["fit_end"] < fold["test_start"]
        selected = predictions.loc[predictions.fold.eq(fold["fold"])]
        assert (selected.train_end < selected.date).all()
        assert selected.train_end.eq(fold["fit_end"]).all()
        preprocessing = json.loads((destination / "models" / f"fold-{fold['fold']}" /
                                    "factor_ridge" / "preprocessing.json").read_text())
        assert preprocessing["fitted_rows"] == fold["train_rows"]
        assert preprocessing["fit_end"] <= fold["train_end"]
    terminal = predictions.loc[predictions.date.isin(data["calendar"][-5:])]
    assert len(terminal) == 3 * 5 * 3  # three models predict every immature row
    assert terminal.label.isna().all()
    for model in report["models"]:
        selected = predictions.loc[predictions.model_name.eq(model["name"])]
        assert model["metrics"]["observations"] == int(selected.label.notna().sum())
        assert model["portfolio"]["trades"]
        assert model["cost_stress_2x"]["fees"] >= model["portfolio"]["metrics"]["fees"]


def test_baseline_and_factor_models_share_identical_oos_population(completed):
    service, report, data = completed
    predictions = pd.read_parquet(service.runs_root / report["run_id"] / "predictions.parquet")
    populations = [group[["date", "symbol"]].reset_index(drop=True)
                   for _, group in predictions.groupby("model_name")]
    for population in populations[1:]:
        pd.testing.assert_frame_equal(populations[0], population)
    assert not ((populations[0].date == data["calendar"][91]) & populations[0].symbol.eq("600000.SH")).any()
    assert not ((populations[0].date == data["calendar"][92]) & populations[0].symbol.eq("000001.SZ")).any()


def test_future_feature_mutation_cannot_change_fitted_state_or_earlier_predictions(tmp_path, monkeypatch):
    data = synthetic_dataset()
    install_data(monkeypatch, data)
    left_service = LearningService(tmp_path / "left")
    left = left_service.run(request(data))
    changed = copy.deepcopy(data)
    cutoff = data["calendar"][105]
    changed["frame"].loc[changed["frame"].date >= cutoff,
                         ["price_a", "price_b", "ma_a", "ma_b", "empty_in_train"]] = 1e9
    install_data(monkeypatch, changed)
    right_service = LearningService(tmp_path / "right")
    right = right_service.run(request(changed))
    left_path, right_path = left_service.runs_root / left["run_id"], right_service.runs_root / right["run_id"]
    for left_model in (left_path / "models").rglob("model.json"):
        right_model = right_path / left_model.relative_to(left_path)
        assert json.loads(left_model.read_text()) == json.loads(right_model.read_text())
    left_scores = pd.read_parquet(left_path / "predictions.parquet")
    right_scores = pd.read_parquet(right_path / "predictions.parquet")
    for field in ("model_name", "fold", "date", "symbol", "score"):
        np.testing.assert_array_equal(left_scores.loc[left_scores.date < cutoff, field],
                                      right_scores.loc[right_scores.date < cutoff, field])


def test_report_index_read_and_sha_validation_are_read_only(completed):
    service, report, _ = completed
    before = {str(path.relative_to(service.root)): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
              for path in service.root.rglob("*") if path.is_file()}
    assert service.list_runs()["runs"][0]["run_id"] == report["run_id"]
    assert service.read_run(report["run_id"]) == report
    service.catalog()
    after = {str(path.relative_to(service.root)): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
             for path in service.root.rglob("*") if path.is_file()}
    assert before == after
    saved = service.runs_root / report["run_id"] / "report.json"
    saved.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        service.read_run(report["run_id"])


def test_gets_on_uninitialized_service_do_not_create_storage(tmp_path):
    root = tmp_path / "uncreated"
    service = LearningService(root)
    assert service.list_runs() == {"runs": []}
    assert service.catalog()["family"] == "cross_sectional_return_v1"
    with pytest.raises(KeyError):
        service.read_run("learn-" + "0" * 32)
    with pytest.raises(ValueError, match="identity"):
        service.read_run("../report")
    assert not root.exists()


@pytest.mark.parametrize("overrides,match", [
    ({"unknown": 1}, "Unknown learning parameters"),
    ({"train_sessions": True}, "train_sessions"),
    ({"symbols": ["600000.SH", "600000.SH"]}, "Duplicate"),
    ({"factor_groups": ["future_secret"]}, "factor_groups"),
    ({"models": ["unimplemented"]}, "models"),
    ({"read_ref": "unsafe"}, "read_ref"),
])
def test_bad_parameters_fail_before_any_storage(tmp_path, overrides, match):
    data = synthetic_dataset()
    params = request(data)
    params.update(overrides)
    service = LearningService(tmp_path / "unused")
    with pytest.raises(ValueError, match=match):
        service.run(params)
    assert not service.root.exists()


def test_cancelled_build_remains_unindexed_and_never_reports_completion(tmp_path, monkeypatch):
    data = synthetic_dataset()
    install_data(monkeypatch, data)
    service = LearningService(tmp_path)
    events, state = [], {"cancelled": False}

    def progress(event):
        events.append(event)
        if event["phase"] == "rolling_train":
            state["cancelled"] = True

    with pytest.raises(JobCancelled):
        service.run(request(data), progress=progress, check_cancel=lambda: state["cancelled"])
    assert service.list_runs() == {"runs": []}
    assert not service.index.exists()
    assert len(list(service.runs_root.glob(".learn-*.building"))) == 1
    assert not list(service.runs_root.glob("learn-*"))
    assert all(event["phase"] != "completed" for event in events)
