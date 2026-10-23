import json
from datetime import date, datetime, timezone

import polars as pl
import pytest

from lake_equity.market.specialties import specialty_view
from lake_equity.market.store import MarketStore
from test_market_store import publish


@pytest.mark.parametrize("section", ["breadth", "valuation", "flows", "industry", "derivatives", "macro", "intraday"])
def test_missing_specialties_are_explicit_and_json_safe(tmp_path, section):
    store = MarketStore(tmp_path)
    snapshot = store.capture()
    result = specialty_view(store, snapshot, section)
    assert result["read_ref"] == snapshot["read_ref"]
    assert result["panels"]
    for panel in result["panels"]:
        assert panel["available"] is False
        assert panel["status"] == "unavailable"
        assert panel["rows"] == []
        assert isinstance(panel["summary"], dict)
        assert isinstance(panel["notes"], list)
    json.dumps(result, allow_nan=False)


def test_valuation_distribution_uses_complete_latest_population(tmp_path):
    store = MarketStore(tmp_path)
    rows = [{"symbol": f"{number:06d}.SH", "trade_date": date(2026, 10, 22), "pe_ttm": 1.0 if number < 100 else 100.0, "pb": 2.0, "source": "eastmoney_datacenter"} for number in range(1200)]
    rows.extend([{"symbol": "negative", "trade_date": date(2026, 10, 22), "pe_ttm": -2.0, "pb": None, "source": "eastmoney_datacenter"}, {"symbol": "missing", "trade_date": date(2026, 10, 22), "pe_ttm": None, "pb": float("nan"), "source": "eastmoney_datacenter"}, {"symbol": "old", "trade_date": date(2026, 10, 21), "pe_ttm": 9999.0, "pb": 9999.0, "source": "eastmoney_datacenter"}])
    publish(store, "valuation_metrics", pl.DataFrame(rows))
    result = specialty_view(store, store.capture(), "valuation")
    panel, preview = result["panels"]
    assert panel["summary"]["population_count"] == 1202
    metric = panel["summary"]["metrics"]["pe_ttm"]
    assert metric["positive_median"] == 100.0
    assert metric["positive_count"] == 1200
    assert metric["nonpositive_count"] == metric["missing_count"] == 1
    assert sum(row["count"] for row in panel["rows"] if row["metric"] == "pe_ttm") == 1202
    assert preview["truncated"] is True
    assert preview["total_count"] == 1202
    assert len(preview["rows"]) == 100
    json.dumps(result, allow_nan=False)


def test_industry_keeps_levels_and_weightings_separate(tmp_path):
    store = MarketStore(tmp_path)
    rows = [{"industry_code": f"{number:06d}", "trade_date": date(2026, 10, 22), "level": level, "weighting": weighting, "ret": number / 10000, "n_members": 10, "n_priced": 8, "n_excluded": 2, "amount": 100.0, "source": "derived"} for level in ("L1", "L2", "L3") for weighting in ("equal", "amount") for number in range(210)]
    publish(store, "industry_index", pl.DataFrame(rows))
    result = specialty_view(store, store.capture(), "industry")
    assert len(result["panels"]) == 6
    assert sum(panel["total_count"] for panel in result["panels"]) == 1260
    for panel in result["panels"]:
        assert panel["summary"]["industry_count"] == 210
        assert panel["summary"]["n_excluded"] == 420
        assert len(panel["rows"]) == 120
        assert panel["truncated"] is True
        assert panel["units"]["ret"] == "fraction"


def test_macro_latest_is_per_indicator_not_global_date(tmp_path):
    store = MarketStore(tmp_path)
    rows = [{"indicator_id": indicator, "obs_date": day, "value": value, "frequency": "monthly", "source": "pboc", "fetched_at": datetime(2026, 10, 22, tzinfo=timezone.utc)} for indicator, day, value in (("old_release", date(2026, 8, 1), 1.0), ("old_release", date(2026, 9, 1), 2.0), ("new_release", date(2026, 10, 1), 3.0))]
    publish(store, "macro_indicators", pl.DataFrame(rows))
    result = specialty_view(store, store.capture(), "macro")
    panel = result["panels"][0]
    assert panel["summary"]["indicator_count"] == 2
    assert {row["indicator_id"]: row["value"] for row in panel["rows"]} == {"old_release": 2.0, "new_release": 3.0}
    assert panel["units"]["value"] == "source_native"


def test_breadth_does_not_fill_missing_metrics_or_dates(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, "market_breadth", pl.DataFrame({"trade_date": [date(2026, 10, 22)], "metric_id": ["advance_count"], "value": [8.0], "source": ["derived"]}))
    panel = specialty_view(store, store.capture(), "breadth")["panels"][0]
    assert panel["summary"]["metrics"] == {"advance_count": 8.0}
    assert panel["total_count"] == 1


def test_flow_families_are_not_merged_or_replaced_with_turnover(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, "sector_bars", pl.DataFrame({"trade_date": [date(2026, 10, 22)], "sector_code": ["881001"], "amount": [999.0], "source": ["ths"]}))
    panels = specialty_view(store, store.capture(), "flows")["panels"]
    assert all(not panel["available"] for panel in panels)
    assert not any(panel["dataset"] == "sector_bars" for panel in panels)


def test_specialty_rendering_never_mutates_retained_snapshot(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, "valuation_metrics", pl.DataFrame({"symbol": ["600000.SH"], "trade_date": [date(2026, 10, 22)], "pe_ttm": [2.0], "pb": [1.0], "source": ["eastmoney_datacenter"]}))
    snapshot = store.capture()
    before = json.dumps(snapshot, sort_keys=True)
    specialty_view(store, snapshot, "valuation")
    assert json.dumps(snapshot, sort_keys=True) == before
    assert specialty_view(store, snapshot, "flows")["section"] == "flows"


def test_northbound_is_explicit_historical_not_live(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, "northbound_flows", pl.DataFrame({"trade_date": [date(2024, 8, 16)], "net_buy": [10.0], "source": ["eastmoney"]}))
    panel = next(panel for panel in specialty_view(store, store.capture(), "flows")["panels"] if panel["dataset"] == "northbound_flows")
    assert panel["status"] == "historical"
    assert panel["summary"]["as_of"] == "2024-08-16"
    assert panel["rows"][0]["net_buy"] == 10.0


def test_intraday_reports_five_configured_symbols_without_fake_prices(tmp_path):
    config = tmp_path / "configs" / "cnequity.toml"
    config.parent.mkdir()
    symbols = ["002436.SZ", "600862.SH", "600869.SH", "000420.SZ", "002156.SZ"]
    config.write_text('[data]\nroot="var/lake"\n[minute_bars]\nscope="watchlist"\nsymbols=' + json.dumps(symbols) + '\n', encoding="utf-8")
    store = MarketStore(tmp_path)
    panels = specialty_view(store, store.capture(), "intraday")["panels"]
    minute = next(panel for panel in panels if panel["dataset"] == "minute_bars")
    assert minute["summary"]["configured_watchlist"] == symbols
    assert len(minute["summary"]["watchlist_status"]) == 5
    assert all(not item["available"] for item in minute["summary"]["watchlist_status"])
    assert minute["rows"] == []


def test_invalid_section_and_tampered_snapshot_fail(tmp_path):
    store = MarketStore(tmp_path)
    snapshot = store.capture()
    with pytest.raises(ValueError, match="Unknown"):
        specialty_view(store, snapshot, "earnings")
    snapshot["captured_at"] = "changed"
    with pytest.raises(ValueError, match="differs"):
        specialty_view(store, snapshot, "breadth")
