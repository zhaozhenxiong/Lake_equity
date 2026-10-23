"""Synthetic published lake fixtures; no production downloads or observations."""
import numpy as np
import pandas as pd
import polars as pl
import pytest

from cnequity.domain.contracts import contract_fingerprint
from cnequity.domain.datasets import DATASETS
from cnequity.storage.revisions import RevisionStore
from lake_equity.learning.dataset import build_dataset
from lake_equity.learning.features import LEGACY_MA, PRICE_VOLUME, trailing_features
from lake_equity.market.store import MarketStore


def publish(store, dataset, frame):
    spec = DATASETS[dataset]
    layer = store.config.derived_root if spec.layer == "derived" else store.config.curated_root
    path = layer / dataset / "synthetic-fixture.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    revisions = RevisionStore(store.config.meta_root, store.config.curated_root, store.config.derived_root)
    revisions.commit(dataset, run_id="synthetic-learning-test", changed_files=[path],
                     schema_version=spec.schema_version, contract_fingerprint=contract_fingerprint(dataset))


@pytest.fixture
def lake(tmp_path):
    store = MarketStore(tmp_path)
    days = pd.bdate_range("2025-01-01", periods=240).date.tolist()
    publish(store, "trading_calendar", pl.DataFrame({"trade_date": days, "is_trading": [True] * len(days)}))
    price = 10 + np.arange(len(days)) * .01
    bars = pd.DataFrame({"symbol": "600000.SH", "trade_date": days, "open": price,
                         "high": price + .1, "low": price - .1, "close": price + .02,
                         "volume": 10000., "amount": price * 10000., "source": "sina_unadjusted_v1"})
    publish(store, "daily_bars", pl.from_pandas(bars))
    return tmp_path, store, days, bars


def spec(days, **kwargs):
    return {"symbols": ["600000.SH"], "start": days[180].isoformat(), "end": days[-1].isoformat(), **kwargs}


def test_label_maturity_and_complete_source_evidence(lake):
    root, _, days, bars = lake
    result = build_dataset(root, spec(days))
    frame = result["frame"]
    assert len(frame) == 60
    assert frame.iloc[0].label == pytest.approx(bars.iloc[185].close / bars.iloc[181].open - 1)
    assert frame.iloc[0].label_end == days[185].isoformat()
    assert frame.tail(5).label.isna().all()
    assert frame.tail(5).label_reason.eq("immature").all()
    assert frame.feature_eligible.all()
    assert len(PRICE_VOLUME) == 38 and len(LEGACY_MA) == 20
    assert result["report"]["group_coverage"]["margin"]["nonempty_values"] == 0
    assert len(result["report"]["dataset_classification"]) == 55
    for value in result["sources"]["datasets"].values():
        assert len(value["receipt_sha256"]) == 64
        assert all(len(f["sha256"]) == 64 for f in value["files"])


def test_calendar_gap_preserved_and_label_does_not_skip_missing_day(lake):
    root, store, days, bars = lake
    publish(store, "daily_bars", pl.from_pandas(bars.drop(index=184)))
    frame = build_dataset(root, spec(days))["frame"].set_index("date")
    assert pd.isna(frame.loc[days[184].isoformat(), "close"])
    assert not frame.loc[days[184].isoformat(), "bar_valid"]
    assert pd.isna(frame.loc[days[180].isoformat(), "label"])
    assert frame.loc[days[180].isoformat(), "label_reason"] == "missing_invalid_or_unverified_bar"
    assert pd.isna(frame.loc[days[190].isoformat(), "ma20_bias"])
    assert not frame.loc[days[190].isoformat(), "feature_eligible"]


def test_future_mutation_cannot_change_prefix_features_or_mature_labels(lake):
    root, store, days, bars = lake
    cutoff = days[210].isoformat()
    before = build_dataset(root, spec(days, end=cutoff))["frame"]
    changed = bars.copy()
    changed.loc[211:, ["open", "high", "low", "close"]] *= 2
    publish(store, "daily_bars", pl.from_pandas(changed))
    after = build_dataset(root, spec(days, end=cutoff))["frame"]
    pd.testing.assert_frame_equal(before, after)
    assert before.tail(5).label.isna().all()


def test_duplicate_published_keys_rejected(lake):
    root, store, days, bars = lake
    publish(store, "daily_bars", pl.from_pandas(pd.concat([bars, bars.iloc[[180]]])))
    with pytest.raises(ValueError, match="Duplicate keys"):
        build_dataset(root, spec(days))


def test_large_raw_gap_is_quarantined(lake):
    root, store, days, bars = lake
    bars.loc[183:, ["open", "high", "low", "close"]] /= 2
    publish(store, "daily_bars", pl.from_pandas(bars))
    frame = build_dataset(root, spec(days))["frame"]
    assert pd.isna(frame.iloc[0].label)
    assert frame.iloc[0].label_reason == "unverified_corporate_action_or_large_gap"
    assert frame.iloc[5].label_reason == "ok"


def test_margin_lags_two_verified_sessions_without_filling_absence(lake):
    root, store, days, _ = lake
    publish(store, "margin_trading", pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [days[185]],
                "margin_balance": [100000.], "margin_buy": [1000.], "short_balance": [None],
                "short_sell_volume": [100.], "source": ["exchange"]}, schema_overrides={"short_balance": pl.Float64}))
    result = build_dataset(root, spec(days))
    frame = result["frame"].set_index("date")
    assert pd.isna(frame.loc[days[185].isoformat(), "margin_buy_to_amount"])
    assert pd.isna(frame.loc[days[186].isoformat(), "margin_buy_to_amount"])
    assert frame.loc[days[187].isoformat(), "margin_buy_to_amount"] > 0
    assert pd.isna(frame.loc[days[188].isoformat(), "margin_buy_to_amount"])
    assert result["report"]["group_coverage"]["margin"]["nonempty_rows"] == 1


def test_snapshot_flow_cannot_backdate_actual_observation(lake):
    root, store, days, _ = lake
    seen = pd.Timestamp(days[190], tz="Asia/Shanghai").tz_convert("UTC").to_pydatetime()
    publish(store, "fund_flow", pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [days[185]],
                "main_net_inflow": [1000.], "large_net_inflow": [300.], "small_net_inflow": [-300.],
                "fetched_at": [seen], "source": ["eastmoney"]}))
    result = build_dataset(root, spec(days, factor_groups=["price_volume", "money_flow"]))
    frame = result["frame"].set_index("date")
    assert frame.loc[:days[190].isoformat(), "flow_main_to_amount"].isna().all()
    assert frame.loc[days[191].isoformat(), "flow_main_to_amount"] > 0
    assert pd.isna(frame.loc[days[192].isoformat(), "flow_main_to_amount"])


def test_snapshot_flow_without_observation_is_not_qualified(lake):
    root, store, days, _ = lake
    publish(store, "fund_flow", pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [days[185]],
                                           "main_net_inflow": [1000.]}))
    result = build_dataset(root, spec(days, factor_groups=["money_flow"]))
    assert result["report"]["group_coverage"]["money_flow"]["nonempty_values"] == 0


def test_mock_funding_source_does_not_enter_features(lake):
    root, store, days, _ = lake
    publish(store, "margin_trading", pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [days[185]],
                                                "margin_balance": [100000.], "margin_buy": [1000.], "source": ["mock"]}))
    result = build_dataset(root, spec(days))
    assert result["report"]["group_coverage"]["margin"]["nonempty_values"] == 0


def test_selected_file_hash_rechecked_after_read(lake):
    root, _, days, _ = lake
    tampered = False
    def progress(**event):
        nonlocal tampered
        if event["phase"] == "dataset_features" and not tampered:
            path = next((root / "var/lake/meta/revisions/data/daily_bars").rglob("*.parquet"))
            payload = bytearray(path.read_bytes())
            payload[-8] ^= 1
            path.write_bytes(payload)
            tampered = True
    with pytest.raises(ValueError, match="source changed"):
        build_dataset(root, spec(days), progress=progress)


def test_cancellation_propagates(lake):
    root, _, days, _ = lake
    def cancel():
        raise RuntimeError("cancelled-test")
    with pytest.raises(RuntimeError, match="cancelled-test"):
        build_dataset(root, spec(days), check_cancel=cancel)


def test_boolean_cancel_stops_before_source_reads(lake):
    from lake_equity.jobs import JobCancelled
    root, _, days, _ = lake
    with pytest.raises(JobCancelled):
        build_dataset(root, spec(days), check_cancel=lambda: True)


def test_migrated_verified_fuyao_source_is_accepted():
    from lake_equity.learning.dataset import RAW_SOURCES
    from my_strategy.services.czsc_trend_support import VERIFIED_SOURCES
    assert 'fuyao_unadjusted_v1' in VERIFIED_SOURCES & RAW_SOURCES


def test_ma_formulas_match_existing_twenty_feature_baseline(lake):
    from my_strategy.services.czsc_market_features import _trailing_features
    _, _, _, bars = lake
    expected = _trailing_features(bars)
    actual = trailing_features(bars)
    np.testing.assert_allclose(actual[LEGACY_MA], expected[LEGACY_MA], rtol=1e-12, atol=1e-12, equal_nan=True)
