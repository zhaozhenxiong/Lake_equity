"""Features frame state continuation: day-append resume, byte-exact prepared frames.

build_features captures its CzscSession and confirmation bookkeeping in a
market_features_state_v1 state (chained input hash proves the observed prefix);
the features cache resumes the newest verified strict-prefix entry, advances
only the appended rows and reruns the cheap whole-frame post-pipeline, so the
stitched prepared frame is byte-identical to a full pipeline build.
"""
import json

import pandas as pd
import pytest

from my_strategy.tests.test_chart_features_cache import _synthetic_frame
from my_strategy.services.czsc_market_features import build_features
from my_strategy.services.native_pack_cache import (
    _pack_path, features_frame_identity, get_or_build_features, put_pack)
from my_strategy.core.run_context import stable_hash
from my_strategy.web_dashboard.trend_strategy import _build_prepared_features


def _json_roundtrip(value):
    return json.loads(json.dumps(value))


def _calendar(frame):
    return [str(day)[:10] for day in frame.date]


def _build(raw, dates):
    return lambda frame, resume_state=None: _build_prepared_features(frame, dates, resume_state)


def test_build_features_resume_delta_matches_full():
    frame = _synthetic_frame(days=320)
    state = _json_roundtrip(build_features(frame.iloc[:260], capture_state=True)[1])
    delta, _ = build_features(frame, resume_state=state, capture_state=True)
    full = build_features(frame)
    pd.testing.assert_frame_equal(delta, full.iloc[260:], check_exact=True)


def test_multi_hop_features_restart_stays_exact():
    frame = _synthetic_frame(days=320)
    full = build_features(frame)
    state = None
    start = 0
    for end in (200, 260, 320):
        window = frame.iloc[:end]
        if state is None:
            delta, state = build_features(window, capture_state=True)
        else:
            delta, state = build_features(window, resume_state=_json_roundtrip(state), capture_state=True)
        pd.testing.assert_frame_equal(delta, full.iloc[start:end], check_exact=True)
        start = end


def test_features_state_tampering_is_rejected():
    frame = _synthetic_frame(days=320)
    state = _json_roundtrip(build_features(frame, capture_state=True)[1])
    state["pen_confirmations"][0][1][0] = "2000-01-01T00:00:00+08:00"
    with pytest.raises(ValueError, match="不匹配"):
        build_features(frame, resume_state=state)


def test_features_state_changed_prefix_is_rejected():
    frame = _synthetic_frame(days=200)
    state = _json_roundtrip(build_features(frame.iloc[:150], capture_state=True)[1])
    changed = frame.copy()
    changed.loc[changed.index[100], "volume"] = float(changed.volume.iloc[100]) * 1.1
    with pytest.raises(ValueError, match="前缀"):
        build_features(changed, resume_state=state)


def test_features_cache_day_append_resume_matches_full(tmp_path):
    frame = _synthetic_frame(days=320)
    dates = _calendar(frame)
    prepared, origin = get_or_build_features(frame.iloc[:260], dates, "ma_trend", _build(frame.iloc[:260], dates), root=tmp_path)
    assert origin == "compute"
    resumed, origin = get_or_build_features(frame, dates, "ma_trend", _build(frame, dates), root=tmp_path)
    assert origin == "compute_incremental"
    full, _ = _build_prepared_features(frame, dates)
    pd.testing.assert_frame_equal(resumed, full, check_exact=True)
    assert resumed.attrs == full.attrs
    again, origin = get_or_build_features(frame, dates, "ma_trend", _build(frame, dates), root=tmp_path)
    assert origin == "cache"
    pd.testing.assert_frame_equal(again, resumed, check_exact=True)


def test_features_cache_changed_prefix_rebuilds(tmp_path):
    frame = _synthetic_frame(days=320)
    dates = _calendar(frame)
    get_or_build_features(frame.iloc[:260], dates, "ma_trend", _build(frame.iloc[:260], dates), root=tmp_path)
    changed = frame.copy()
    changed.loc[changed.index[40], "volume"] = float(changed.volume.iloc[40]) * 1.01
    prepared, origin = get_or_build_features(changed, dates, "ma_trend", _build(changed, dates), root=tmp_path)
    assert origin == "compute"
    full, _ = _build_prepared_features(changed, dates)
    pd.testing.assert_frame_equal(prepared, full, check_exact=True)


def test_features_cache_missing_candidate_falls_back(tmp_path):
    frame = _synthetic_frame(days=320)
    dates = _calendar(frame)
    get_or_build_features(frame.iloc[:260], dates, "ma_trend", _build(frame.iloc[:260], dates), root=tmp_path)
    identity = features_frame_identity(frame.iloc[:260], dates, "ma_trend")
    _pack_path(tmp_path, stable_hash(identity)).unlink()
    prepared, origin = get_or_build_features(frame, dates, "ma_trend", _build(frame, dates), root=tmp_path)
    assert origin == "compute"
    full, _ = _build_prepared_features(frame, dates)
    pd.testing.assert_frame_equal(prepared, full, check_exact=True)


def test_features_cache_reads_legacy_bare_frame_entries(tmp_path):
    frame = _synthetic_frame(days=140)
    dates = _calendar(frame)
    bare, _ = _build_prepared_features(frame, dates)
    put_pack(features_frame_identity(frame, dates, "ma_trend"), bare, root=tmp_path)

    def _must_not_build(frame, resume_state=None):
        raise AssertionError("legacy entry must be served from cache")

    prepared, origin = get_or_build_features(frame, dates, "ma_trend", _must_not_build, root=tmp_path)
    assert origin == "cache"
    pd.testing.assert_frame_equal(prepared, bare)
