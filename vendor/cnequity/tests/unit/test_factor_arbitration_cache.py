"""A cached peer opinion must cover the sessions being arbitrated."""

import json
from datetime import date

import polars as pl

from cnequity.config import Config
from cnequity.derive.factor_arbitration import _baostock_factors, _cache_batch

SYMBOL = "600001.SH"
FIRST = date(2024, 1, 2)
LATER = date(2024, 1, 3)


def _events(*levels):
    return pl.DataFrame(
        [(SYMBOL, day, level) for day, level in levels],
        schema={"symbol": pl.String, "trade_date": pl.Date, "factor": pl.Float64},
        orient="row",
    )


def _no_fetch(*args, **kwargs):
    raise AssertionError("Complete cached evidence should be reused")


def test_new_session_requires_new_evidence_but_same_window_reuses_cache(tmp_path):
    cfg = Config(data_root=tmp_path)
    asked = []

    def fetch(names, start, end, *, config):
        asked.append(end)
        frame = _events((FIRST, 1.0), (LATER, 1.1))
        return frame.filter(pl.col("trade_date") <= end), []

    _baostock_factors(cfg, [SYMBOL], FIRST, fetch)
    _baostock_factors(cfg, [SYMBOL], FIRST, _no_fetch)
    frame, failed = _baostock_factors(cfg, [SYMBOL], LATER, fetch)
    assert asked == [FIRST, LATER]
    assert not failed
    assert frame.equals(_events((FIRST, 1.0), (LATER, 1.1)))
    again, _ = _baostock_factors(cfg, [SYMBOL], LATER, _no_fetch)
    assert again.equals(frame)


def test_latest_complete_snapshot_replaces_events_in_older_batch(tmp_path):
    cfg = Config(data_root=tmp_path)
    _cache_batch(cfg, _events((FIRST, 1.0), (LATER, 1.1)), [SYMBOL], LATER)
    # The provider corrected away an event. A union or date-level dedupe
    # would retain the old event and manufacture a step in the peer series.
    corrected = _events((FIRST, 1.0))
    _cache_batch(cfg, corrected, [SYMBOL], LATER)
    frame, failed = _baostock_factors(cfg, [SYMBOL], LATER, _no_fetch)
    assert not failed
    assert frame.equals(corrected)


def test_failed_partial_answer_is_excluded_and_retried(tmp_path):
    cfg = Config(data_root=tmp_path)

    def partial(*args, **kwargs):
        return _events((FIRST, 1.0)), [SYMBOL]

    frame, failed = _baostock_factors(cfg, [SYMBOL], LATER, partial)
    assert frame.is_empty()
    assert failed == [SYMBOL]
    complete = _events((FIRST, 1.0), (LATER, 1.1))
    frame, failed = _baostock_factors(cfg, [SYMBOL], LATER, lambda *args, **kwargs: (complete, []))
    assert not failed
    assert frame.equals(complete)
    cached, _ = _baostock_factors(cfg, [SYMBOL], LATER, _no_fetch)
    assert cached.equals(complete)


def test_legacy_cache_without_query_window_cannot_prove_coverage(tmp_path):
    cfg = Config(data_root=tmp_path)
    _cache_batch(cfg, _events((FIRST, 1.0)), [SYMBOL], LATER)
    sidecar = next((cfg.meta_root / "adj_factor_arbitration_cache").glob("*.json"))
    meta = json.loads(sidecar.read_text())
    del meta["start"], meta["end"]
    sidecar.write_text(json.dumps(meta))
    calls = []

    def fetch(names, *args, **kwargs):
        calls.extend(names)
        return _events((FIRST, 1.0), (LATER, 1.1)), []

    frame, _ = _baostock_factors(cfg, [SYMBOL], LATER, fetch)
    assert calls == [SYMBOL]
    assert frame.height == 2
