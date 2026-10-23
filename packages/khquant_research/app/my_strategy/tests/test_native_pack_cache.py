# -*- coding: utf-8 -*-
"""Native pack cache: content identity, round-trip fidelity, GC, contract parity."""
from __future__ import annotations

import os
import time

import pandas as pd
import pytest

from my_strategy.services import native_pack_cache
from my_strategy.services.czsc_native_evidence import build_native_evidence, native_contract


def _frame(*, rows=8, date_as_string=True, close_offset=0.0):
    dates = pd.date_range("2026-01-05", periods=rows, freq="D")
    frame = pd.DataFrame({
        "symbol": "000001.SZ",
        "date": dates.strftime("%Y-%m-%d") if date_as_string else dates,
        "open": 10.0, "high": 10.2, "low": 9.8,
        "close": [10.0 + close_offset + i * 0.1 for i in range(rows)],
        "volume": 1e7, "amount": 1e8,
        "source": "tushare", "has_trade_price": 1,
        "reason_codes": [["verified"]] * rows,
    })
    frame.attrs["data_version"] = "test"
    return frame


def _pack(frame, marker="fresh"):
    return {"frame": frame.copy(), "events": [{"date": str(frame.date.iloc[-1])[:10], "kind": marker}],
            "evaluations": [], "wyckoff_events": [], "ict_events": [],
            "version": "native_price_volume_evidence"}


def test_identity_is_content_addressed(tmp_path):
    frame = _frame()
    identity = native_pack_cache.native_pack_identity(frame, ["2026-01-05"])
    same = native_pack_cache.native_pack_identity(frame.copy(), ["2026-01-05"])
    assert identity == same
    changed = native_pack_cache.native_pack_identity(_frame(close_offset=1.0), ["2026-01-05"])
    assert changed["frame_sha256"] != identity["frame_sha256"]
    other_calendar = native_pack_cache.native_pack_identity(frame, ["2026-01-06"])
    assert other_calendar["calendar_sha256"] != identity["calendar_sha256"]


def test_frame_sha256_handles_list_cells():
    frame = _frame()
    first = native_pack_cache.frame_sha256(frame)
    altered = frame.copy()
    altered["reason_codes"] = [["different"]] * len(altered)
    assert native_pack_cache.frame_sha256(altered) != first


def test_get_or_build_caches_and_returns_independent_copies(tmp_path):
    frame = _frame()
    calls = []

    def build(canonical):
        calls.append(canonical)
        return _pack(canonical)

    pack, origin = native_pack_cache.get_or_build(frame, ["2026-01-05"], build, root=tmp_path)
    assert origin == "compute" and len(calls) == 1
    # The builder receives the canonical (ISO-string date) frame.
    assert isinstance(calls[0].date.iloc[0], str) and calls[0].date.iloc[0] == "2026-01-05"
    again, origin = native_pack_cache.get_or_build(frame, ["2026-01-05"], build, root=tmp_path)
    assert origin == "cache" and len(calls) == 1
    pd.testing.assert_frame_equal(pack["frame"], again["frame"])
    assert pack["events"] == again["events"]
    # A datetime-typed request frame hits the same identity after canonicalization.
    typed, origin = native_pack_cache.get_or_build(
        _frame(date_as_string=False), ["2026-01-05"], build, root=tmp_path)
    assert origin == "cache" and len(calls) == 1
    # Mutating a returned pack never pollutes the store.
    again["events"].append({"kind": "tampered"})
    third, _ = native_pack_cache.get_or_build(frame, ["2026-01-05"], build, root=tmp_path)
    assert third["events"] == pack["events"]


def test_get_pack_drops_corrupt_payload(tmp_path):
    frame = _frame()
    identity = native_pack_cache.native_pack_identity(frame, ["2026-01-05"])
    key = native_pack_cache.stable_hash(identity)
    path = native_pack_cache._pack_path(tmp_path, key)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a gzip pickle")
    assert native_pack_cache.get_pack(identity, root=tmp_path) is None
    assert not path.exists()


def test_gc_evicts_oldest_and_sweeps_tmp_and_empty_shards(tmp_path):
    frame = _frame()
    identity = native_pack_cache.native_pack_identity(frame, ["2026-01-05"])
    paths = []
    for offset in range(3):
        changed = dict(identity)
        changed["frame_sha256"] = f"{offset:064d}"
        blob = native_pack_cache.gzip.compress(
            native_pack_cache.pickle.dumps({"format": native_pack_cache.FORMAT,
                                            "identity": changed, "pack": _pack(frame)}))
        key = native_pack_cache.stable_hash(changed)
        path = native_pack_cache._pack_path(tmp_path, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        old = time.time() - (3 - offset) * 100
        os.utime(path, (old, old))
        paths.append((path, len(blob)))
    stale_tmp = paths[0][0].with_name("stale.tmp-1-1")
    stale_tmp.write_bytes(b"partial")
    ancient = time.time() - 7200
    os.utime(stale_tmp, (ancient, ancient))
    total = sum(size for _, size in paths)
    report = native_pack_cache.gc_native_packs(root=tmp_path, quota_bytes=total - 1)
    assert report["removed"] >= 2  # oldest pack + stale tmp
    assert not paths[0][0].exists()
    assert paths[-1][0].exists()
    assert not stale_tmp.exists()
    # No shard directory is left empty.
    for shard in tmp_path.iterdir():
        if shard.is_dir():
            assert any(shard.iterdir())


def test_native_contract_matches_build_hashes():
    contract = native_contract()
    empty = pd.DataFrame({"symbol": [], "date": []})
    pack = build_native_evidence(empty)
    assert pack["config_hash"] == contract["config_hash"]
    assert pack["schema_hash"] == contract["schema_hash"]
    assert pack["semantics"] == contract["semantics"]
    for key in ("settings", "czsc_settings", "ict_settings"):
        assert key in contract
