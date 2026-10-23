"""Chart payload and feature frame caches (next-round compute optimization).

The chart cache stores the UNFILTERED CzscSession payload keyed by
(frame, settings, code); filter_chart_payload must reproduce payload(start)
display clipping byte-for-byte so one entry serves every chart start. The
feature cache stores prepare_observed_data's feature frame keyed by
(augmented raw, calendar, profile, code).
"""
import numpy as np
import pandas as pd
import pytest

from my_strategy.services import native_pack_cache
from my_strategy.services.czsc_analysis import CzscSession, filter_chart_payload, strategy_config


def _synthetic_frame(days=320, seed=7):
    dates = pd.bdate_range("2025-01-01", periods=days)
    rng = np.random.default_rng(seed)
    close = 10 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, size=days)))
    open_ = close * (1 + rng.normal(0, 0.004, size=days))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, size=days))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, size=days))
    volume = rng.uniform(1e6, 3e6, size=days)
    return pd.DataFrame({
        "symbol": "000001.SZ", "date": dates, "dt": dates + pd.Timedelta(hours=15),
        "id": np.arange(days), "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": volume * close, "source": "tushare", "has_trade_price": 1,
    })


def _advance(frame, settings):
    session = CzscSession(str(frame.iloc[0].symbol), settings, len(frame), create_position=False)
    for row in frame.itertuples(index=False):
        session.advance_structure(row)
    return session


@pytest.mark.parametrize("start", [None, "2025-01-02", "2025-02-03", "2025-08-01", "2026-01-01"])
def test_filter_chart_payload_matches_payload_start(start):
    frame = _synthetic_frame()
    session = _advance(frame, strategy_config(None))
    full = session.payload(None)
    last_day = str(frame.iloc[-1].date)[:10]
    for value in ({start, last_day} - {None} | {start}):
        expected = session.payload(value)
        assert filter_chart_payload(full, value) == expected
    assert filter_chart_payload(full, None) is full


def test_filter_chart_payload_zone_direction_uses_full_history():
    frame = _synthetic_frame()
    session = _advance(frame, strategy_config(None))
    full = session.payload(None)
    clipped = filter_chart_payload(full, "2026-01-01")
    expected = session.payload("2026-01-01")
    for frequency in clipped:
        assert [zone["direction"] for zone in clipped[frequency]["zones"]] == \
               [zone["direction"] for zone in expected[frequency]["zones"]]


def test_chart_payload_cache_roundtrip_and_isolation(tmp_path):
    frame = _synthetic_frame(days=140)
    settings = strategy_config(None)
    calls = []

    def build():
        calls.append(1)
        return _advance(frame, settings).payload(None)

    root = tmp_path / "packs"
    first, origin = native_pack_cache.get_or_build_chart_payload(frame, settings, build, root=root)
    assert origin == "compute" and len(calls) == 1
    second, origin = native_pack_cache.get_or_build_chart_payload(frame, settings, build, root=root)
    assert origin == "cache" and len(calls) == 1
    assert second == first
    # Callers may mutate the returned payload; the stored bytes never alias it.
    second["日线"]["bars"].clear()
    third, origin = native_pack_cache.get_or_build_chart_payload(frame, settings, build, root=root)
    assert origin == "cache" and third == first and len(third["日线"]["bars"]) == len(first["日线"]["bars"])


def test_chart_payload_identity_tracks_frame_and_settings(tmp_path):
    frame = _synthetic_frame(days=140)
    settings = strategy_config(None)
    root = tmp_path / "packs"
    calls = []

    def build():
        calls.append(1)
        return {"frequencies": len(calls)}

    native_pack_cache.get_or_build_chart_payload(frame, settings, build, root=root)
    changed = frame.copy()
    changed.loc[changed.index[-1], "close"] = float(changed.iloc[-1].close) * 1.01
    _, origin = native_pack_cache.get_or_build_chart_payload(changed, settings, build, root=root)
    assert origin == "compute"
    other_settings = dict(settings, min_bi_len=int(settings["min_bi_len"]) + 1)
    _, origin = native_pack_cache.get_or_build_chart_payload(frame, other_settings, build, root=root)
    assert origin == "compute"
    assert len(calls) == 3


def test_features_frame_cache_roundtrip_attrs_and_isolation(tmp_path):
    frame = _synthetic_frame(days=30)
    calendar = [str(day)[:10] for day in frame.date]
    calls = []

    def build(frame, resume_state=None):
        calls.append(1)
        features = pd.DataFrame({"date": frame.date.dt.strftime("%Y-%m-%d"),
                                 "close": frame.close.to_numpy(), "ret_1": np.linspace(-.01, .02, len(frame))})
        features.attrs.update(profile="ma_trend", calendar_hash="cal", marker=len(calls))
        return features, None

    root = tmp_path / "packs"
    first, origin = native_pack_cache.get_or_build_features(frame, calendar, "ma_trend", build, root=root)
    assert origin == "compute"
    second, origin = native_pack_cache.get_or_build_features(frame, calendar, "ma_trend", build, root=root)
    assert origin == "cache" and len(calls) == 1
    pd.testing.assert_frame_equal(second, first)
    assert second.attrs == first.attrs == {"profile": "ma_trend", "calendar_hash": "cal", "marker": 1}
    # prepare_observed_data copies raw-only columns into the cached frame; the
    # stored object must stay clean for the next reader.
    second["extra_raw_column"] = 1.0
    third, origin = native_pack_cache.get_or_build_features(frame, calendar, "ma_trend", build, root=root)
    assert origin == "cache" and "extra_raw_column" not in third.columns
    # Calendar or profile changes are identity changes.
    _, origin = native_pack_cache.get_or_build_features(frame, calendar[:-1], "ma_trend", build, root=root)
    assert origin == "compute"
    _, origin = native_pack_cache.get_or_build_features(frame, calendar, "other_profile", build, root=root)
    assert origin == "compute"
    assert len(calls) == 3


def test_pack_quota_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_QUOTA_BYTES", "4096")
    assert native_pack_cache.default_quota_bytes() == 4096
    monkeypatch.setenv("KHQUANT_NATIVE_PACK_QUOTA_BYTES", "not-a-number")
    assert native_pack_cache.default_quota_bytes() == native_pack_cache.DEFAULT_QUOTA_BYTES
    monkeypatch.delenv("KHQUANT_NATIVE_PACK_QUOTA_BYTES")
    assert native_pack_cache.default_quota_bytes() == native_pack_cache.DEFAULT_QUOTA_BYTES


def test_store_kinds_share_quota_without_collision(tmp_path):
    frame = _synthetic_frame(days=30)
    calendar = [str(day)[:10] for day in frame.date]
    settings = strategy_config(None)
    root = tmp_path / "packs"
    chart_identity = native_pack_cache.chart_payload_identity(frame, settings)
    features_identity = native_pack_cache.features_frame_identity(frame, calendar, "ma_trend")
    native_identity = native_pack_cache.native_pack_identity(
        native_pack_cache.canonical_frame(frame), calendar)
    keys = {native_pack_cache.stable_hash(identity) for identity in
            (chart_identity, features_identity, native_identity)}
    assert len(keys) == 3
    native_pack_cache.put_pack(chart_identity, {"kind": "chart"}, root=root)
    native_pack_cache.put_pack(features_identity, {"kind": "features"}, root=root)
    assert native_pack_cache.get_pack(chart_identity, root=root) == {"kind": "chart"}
    assert native_pack_cache.get_pack(features_identity, root=root) == {"kind": "features"}
    # An identity that was never stored misses even though the frame matches.
    assert native_pack_cache.get_pack(native_identity, root=root) is None
