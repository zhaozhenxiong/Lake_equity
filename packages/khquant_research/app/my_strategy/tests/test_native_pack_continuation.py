"""Pack-level verified continuation: day-append resumes, never serves drift.

build_native_evidence's incremental_state contract is proven in
tests/test_incremental_native_state.py; these tests prove the pack store
wires it correctly: stitched packs equal full replays, stale candidates are
rejected, and every doubt falls back to a full build. Production callers pass
the full verified calendar regardless of frame length, as these tests do.
"""
import json

import pandas as pd
import pytest

from my_strategy.tests.test_current_native_evidence import _raw
from my_strategy.services.czsc_native_evidence import build_native_evidence
from my_strategy.services.native_pack_cache import (
    _pack_path, get_or_build_native, native_pack_identity)
from my_strategy.core.run_context import stable_hash


def _canon(bars):
    return bars.assign(date=bars.date.dt.strftime("%Y-%m-%d"))


def _assert_pack_equal(stitched, full):
    pd.testing.assert_frame_equal(stitched["frame"], full["frame"], check_exact=True)
    assert stitched["events"] == full["events"]
    assert stitched["evaluations"] == full["evaluations"]
    assert stitched["wyckoff_events"] == full["wyckoff_events"]
    assert stitched["ict_events"] == full["ict_events"]
    assert stitched["auxiliary_evaluations"] == full["auxiliary_evaluations"]
    assert stitched["incremental_state"] == full["incremental_state"]
    for key in ("version", "calendar_provenance", "config_hash", "schema_hash", "semantics"):
        assert stitched[key] == full[key]


def test_day_append_resume_matches_full_replay(tmp_path):
    bars = _raw(220, varied=True)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    pack, origin = get_or_build_native(bars.iloc[:151], dates, root=tmp_path)
    assert origin == "compute"
    extended, origin = get_or_build_native(bars, dates, root=tmp_path)
    assert origin == "compute_incremental"
    _assert_pack_equal(extended, build_native_evidence(_canon(bars), market_dates=dates, capture_state=True))


def test_multi_hop_continuation_stays_exact(tmp_path):
    bars = _raw(220, varied=True)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    get_or_build_native(bars.iloc[:121], dates, root=tmp_path)
    get_or_build_native(bars.iloc[:187], dates, root=tmp_path)
    pack, origin = get_or_build_native(bars, dates, root=tmp_path)
    assert origin == "compute_incremental"
    _assert_pack_equal(pack, build_native_evidence(_canon(bars), market_dates=dates, capture_state=True))


def test_same_frame_is_a_plain_cache_hit(tmp_path):
    bars = _raw(180, varied=True)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    get_or_build_native(bars, dates, root=tmp_path)
    again, origin = get_or_build_native(bars, dates, root=tmp_path)
    assert origin == "cache"
    pd.testing.assert_frame_equal(again["frame"],
                                  build_native_evidence(_canon(bars), market_dates=dates)["frame"], check_exact=True)


def test_changed_prefix_rejects_resume_and_rebuilds(tmp_path):
    bars = _raw(180, varied=True)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    get_or_build_native(bars.iloc[:151], dates, root=tmp_path)
    changed = bars.copy()
    changed.loc[changed.index[40], "volume"] = changed.volume.iloc[40] + 1
    pack, origin = get_or_build_native(changed, dates, root=tmp_path)
    assert origin == "compute"
    _assert_pack_equal(pack, build_native_evidence(_canon(changed), market_dates=dates, capture_state=True))


def test_missing_candidate_pack_falls_back_and_prunes(tmp_path):
    bars = _raw(180, varied=True)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    get_or_build_native(bars.iloc[:151], dates, root=tmp_path)
    identity = native_pack_identity(
        bars.iloc[:151].assign(date=bars.date.iloc[:151].dt.strftime("%Y-%m-%d")), dates)
    _pack_path(tmp_path, stable_hash(identity)).unlink()
    pack, origin = get_or_build_native(bars, dates, root=tmp_path)
    assert origin == "compute"
    _assert_pack_equal(pack, build_native_evidence(_canon(bars), market_dates=dates, capture_state=True))
    index_file = next((tmp_path / "continuation").glob("*.json"))
    entries = json.loads(index_file.read_text(encoding="utf-8"))["entries"]
    assert [entry["rows"] for entry in entries] == [len(bars)]


def test_same_length_new_content_does_not_resume(tmp_path):
    bars = _raw(180, varied=True)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    get_or_build_native(bars, dates, root=tmp_path)
    changed = bars.copy()
    changed.loc[changed.index[-1], "volume"] = changed.volume.iloc[-1] + 1
    pack, origin = get_or_build_native(changed, dates, root=tmp_path)
    assert origin == "compute"
    _assert_pack_equal(pack, build_native_evidence(_canon(changed), market_dates=dates, capture_state=True))


def test_earlier_cutoff_served_from_longer_pack_verified(tmp_path):
    bars = _raw(220, varied=True)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    get_or_build_native(bars, dates, root=tmp_path)
    sliced, origin = get_or_build_native(bars.iloc[:151], dates, root=tmp_path)
    assert origin == "cache_prefix"
    assert "incremental_state" not in sliced  # serve-only; never a resume source
    reference = build_native_evidence(_canon(bars.iloc[:151]), market_dates=dates)
    pd.testing.assert_frame_equal(sliced["frame"], reference["frame"], check_exact=True)
    assert sliced["events"] == reference["events"]
    assert sliced["evaluations"] == reference["evaluations"]
    assert sliced["wyckoff_events"] == reference["wyckoff_events"]
    assert sliced["ict_events"] == reference["ict_events"]
    assert sliced["auxiliary_evaluations"] == reference["auxiliary_evaluations"]


def test_longer_pack_with_different_content_is_not_served(tmp_path):
    bars = _raw(220, varied=True)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    get_or_build_native(bars, dates, root=tmp_path)
    changed = bars.copy()
    changed.loc[changed.index[40], "volume"] = changed.volume.iloc[40] + 1
    pack, origin = get_or_build_native(changed.iloc[:151], dates, root=tmp_path)
    assert origin == "compute"
    reference = build_native_evidence(_canon(changed.iloc[:151]), market_dates=dates, capture_state=True)
    _assert_pack_equal(pack, reference)
