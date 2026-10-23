"""The init prefetch warms the Sina factor cache and never gates a build."""

from __future__ import annotations

from datetime import date

import httpx
import polars as pl

import cnequity.derive.adj_factors as adj
from cnequity.config import Config
from cnequity.orchestrator.init_phases import expected_steps


def _series(symbol: str) -> pl.DataFrame:
    return pl.DataFrame({"trade_date": [date(2024, 1, 2)], "factor": [1.5]})


def test_prefetch_caches_only_what_is_missing_and_swallows_failures(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data")
    adj._save_cache(cfg, "600519.SH", adj.STORED_ADJUST_TYPE, _series("600519.SH"))
    asked: list[str] = []

    def fetch(symbol, adjust_type, *, client):
        asked.append(symbol)
        if symbol == "000002.SZ":
            raise httpx.ConnectError("down")
        return _series(symbol)

    monkeypatch.setattr(adj, "fetch_adj_factor_series", fetch)
    stats = adj.prefetch_factor_caches(cfg, ["600519.SH", "000001.SZ", "000002.SZ", "000001.SZ"])

    assert sorted(asked) == ["000001.SZ", "000002.SZ"]
    assert stats == {"requested": 2, "cached": 1, "skipped": 1, "failed": 1, "left": 0}
    assert adj._load_cache(cfg, "000001.SZ", adj.STORED_ADJUST_TYPE).height == 1
    assert adj._load_cache(cfg, "000002.SZ", adj.STORED_ADJUST_TYPE) is None


def test_prefetch_asks_nothing_when_sina_is_not_the_source(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data")
    cfg.sources["sina"] = False
    monkeypatch.setattr(
        adj, "fetch_adj_factor_series", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    assert adj.prefetch_factor_caches(cfg, ["000001.SZ"])["skipped"] == 1


def test_a_stopped_prefetch_leaves_the_rest_to_the_derive(tmp_path, monkeypatch):
    import threading

    cfg = Config(data_root=tmp_path / "data")
    stop = threading.Event()
    asked: list[str] = []

    def fetch(symbol, adjust_type, *, client):
        asked.append(symbol)
        stop.set()
        return _series(symbol)

    monkeypatch.setattr(adj, "PREFETCH_WORKERS", 1)
    monkeypatch.setattr(adj, "fetch_adj_factor_series", fetch)
    stats = adj.prefetch_factor_caches(cfg, ["000001.SZ", "000002.SZ", "000004.SZ"], stop=stop)
    assert asked == ["000001.SZ"]
    assert stats["cached"] == 1 and stats["requested"] == 1 and stats["left"] == 2


def test_the_prefetch_is_never_an_owed_init_step():
    from cnequity.orchestrator.init_phases import DEFAULT_INIT_PHASES

    assert "adj_factors_prefetch" not in expected_steps(DEFAULT_INIT_PHASES)
