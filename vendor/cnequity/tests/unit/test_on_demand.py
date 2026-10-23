"""Offline coverage for OnDemandService cache + unimplemented stubs."""

from __future__ import annotations

import pytest

from cnequity.config import Config
from cnequity.query.on_demand import OnDemandService


def test_fetch_cache_roundtrip(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", on_demand_datasets=["stock_news"])
    svc = OnDemandService(cfg)

    monkeypatch.setattr(
        "cnequity.query.on_demand.fetch_stock_news",
        lambda symbol, **k: {"symbol": symbol, "items": [{"title": "t"}]},
    )
    monkeypatch.setattr(Config, "rate_limit", lambda self, name: None)

    first = svc.fetch("stock_news", "600519.SH", limit=5)
    assert first["items"][0]["title"] == "t"
    assert first["data_version"] == "v1"

    # Second hit uses cache (remote would raise if called).
    monkeypatch.setattr(
        "cnequity.query.on_demand.fetch_stock_news",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    second = svc.fetch("stock_news", "600519.SH", limit=5)
    assert second["items"][0]["title"] == "t"

    with pytest.raises(ValueError, match="not enabled"):
        svc.fetch("unknown_ds", "600519.SH")


def test_corrupt_cache_is_refetched(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", on_demand_datasets=["stock_news"])
    svc = OnDemandService(cfg)
    path = cfg.meta_root / "on_demand" / "stock_news" / "600519_SH.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{truncated", encoding="utf-8")

    monkeypatch.setattr(
        "cnequity.query.on_demand.fetch_stock_news",
        lambda symbol, **k: {"symbol": symbol, "items": [{"title": "fresh"}]},
    )
    monkeypatch.setattr(Config, "rate_limit", lambda self, name: None)

    out = svc.fetch("stock_news", "600519.SH")

    assert out["items"][0]["title"] == "fresh"


def test_parameterised_stock_news_queries_do_not_share_cache(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", on_demand_datasets=["stock_news"])
    svc = OnDemandService(cfg)
    calls = []

    def fetch(symbol, **kwargs):
        calls.append(kwargs)
        return {"symbol": symbol, "items": [{"limit": kwargs["limit"]}]}

    monkeypatch.setattr("cnequity.query.on_demand.fetch_stock_news", fetch)
    monkeypatch.setattr(Config, "rate_limit", lambda self, name: None)

    assert svc.fetch("stock_news", "600519.SH", limit=5)["items"][0]["limit"] == 5
    assert svc.fetch("stock_news", "600519.SH", limit=30)["items"][0]["limit"] == 30
    assert [call["limit"] for call in calls] == [5, 30]


def test_cache_symbol_is_kept_inside_on_demand_root(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", on_demand_datasets=["stock_news"])
    svc = OnDemandService(cfg)
    monkeypatch.setattr(
        "cnequity.query.on_demand.fetch_stock_news",
        lambda symbol, **kwargs: {"symbol": symbol, "items": []},
    )
    monkeypatch.setattr(Config, "rate_limit", lambda self, name: None)

    svc.fetch("stock_news", "../outside/600519.SH")

    files = list((cfg.meta_root / "on_demand" / "stock_news").glob("*.json"))
    assert len(files) == 1
    assert files[0].resolve().is_relative_to((cfg.meta_root / "on_demand").resolve())


def test_unimplemented_datasets_raise_and_do_not_cache(tmp_path):
    cfg = Config(
        data_root=tmp_path / "data",
        on_demand_datasets=["announcement_body", "financial_reports"],
    )
    svc = OnDemandService(cfg)

    with pytest.raises(NotImplementedError, match="announcement_body"):
        svc.fetch("announcement_body", "600519.SH")
    assert not (cfg.meta_root / "on_demand" / "announcement_body").exists()

    with pytest.raises(NotImplementedError, match="financial_reports"):
        svc.fetch("financial_reports", "600519.SH")
    assert not (cfg.meta_root / "on_demand" / "financial_reports").exists()


def test_research_reports_error_path_skips_cache(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", on_demand_datasets=["research_reports"])
    svc = OnDemandService(cfg)
    monkeypatch.setattr(Config, "rate_limit", lambda self, name: None)

    class BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url):
            raise RuntimeError("offline")

    monkeypatch.setattr("cnequity.query.on_demand.EastMoneyClient", BoomClient)
    out = svc.fetch("research_reports", "600519.SH")
    assert out["items"] == []
    assert "error" in out
    assert not (cfg.meta_root / "on_demand" / "research_reports").exists()

    with pytest.raises(NotImplementedError, match="mystery"):
        svc._fetch_remote("mystery", "600519.SH")


def test_disabled_on_demand_never_fetches(tmp_path, monkeypatch):
    svc = OnDemandService(Config(data_root=tmp_path, on_demand_enabled=False))
    monkeypatch.setattr(svc, "_fetch_remote", lambda *a, **k: pytest.fail("remote fetch"))
    with pytest.raises(ValueError, match="disabled"):
        svc.fetch("stock_news", "600519.SH", refresh=True)


def test_disabled_research_report_source_never_fetches(tmp_path, monkeypatch):
    svc = OnDemandService(Config(data_root=tmp_path, sources={"eastmoney": False}))
    monkeypatch.setattr(
        "cnequity.query.on_demand.EastMoneyClient", lambda **k: pytest.fail("remote client")
    )
    with pytest.raises(RuntimeError, match="source disabled"):
        svc.fetch("research_reports", "600519.SH")


def test_dataset_path_is_validated_before_cache_access(tmp_path):
    svc = OnDemandService(Config(data_root=tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "600519_SH.json").write_text('{"private": true}')
    with pytest.raises(NotImplementedError):
        svc.fetch(str(outside), "600519.SH")
