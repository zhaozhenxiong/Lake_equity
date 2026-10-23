from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lake_equity.market.engine import FEATURE_COLUMNS, MarketFeaturesService


class FixtureStore:
    def __init__(self, root, *, members=None, prices=None):
        self.root = root
        self.dates = [date(2026, 1, 1) + timedelta(days=position) for position in range(85)]
        self.symbols = [f"{position:06d}.SZ" for position in range(1, 7)]
        self.frames = {
            "trading_calendar": pl.DataFrame({"trade_date": self.dates, "is_trading": [True] * len(self.dates)}),
            "sector_members": pl.DataFrame(members or [{"symbol": symbol, "sector_code": "B1", "sector_name": "Example",
                "as_of_date": self.dates[-1], "source": "eastmoney"} for symbol in self.symbols]),
            "daily_bars": pl.DataFrame(prices or [{"symbol": symbol, "trade_date": day, "open": 10. + position,
                "high": 11. + position, "low": 9. + position, "close": 10. + position,
                "volume": 1000, "amount": float((position + 1) * 100), "source": "fixture_observation"}
                for position, day in enumerate(self.dates) for symbol in self.symbols]),
            "sector_bars": pl.DataFrame([{"sector_code": "881001", "sector_name": "Provider board", "board_type": "industry",
                "trade_date": day, "open": 100. + position, "high": 102. + position, "low": 99. + position,
                "close": 101. + position, "volume": 1, "amount": float(position + 1), "source": "ths"}
                for position, day in enumerate(self.dates)]),
        }
        self.ref = "a" * 64
        self.calls = []

    def capture(self, read_ref=None):
        self.calls.append(("capture", read_ref))
        return {"read_ref": self.ref, "datasets": {name: {"available": True, "units": {"amount": "source_native" if name == "sector_bars" else "CNY"}, "run_id": None}
            for name in self.frames}}

    def scan(self, name, snapshot):
        self.calls.append(("scan", name))
        return self.frames[name].lazy()


def test_member_formulas_previous20_and_immutable_reads(tmp_path):
    store = FixtureStore(tmp_path)
    service = MarketFeaturesService(store)
    assert service.status() is None
    view = service.prepare()
    board = view["boards"][0]
    assert view["training_eligible"] is False
    assert view["pit_eligible"] is False
    assert view["units"]["amount"] == "CNY"
    assert board["kind"] == "mixed"
    assert set(FEATURE_COLUMNS) <= board.keys()
    assert board["amount"] == 6 * 8500.
    assert board["amount_change"] == 600.
    assert board["amount_change_pct"] == pytest.approx(1 / 84)
    assert board["relative_amount20"] == pytest.approx(85 / 74.5)
    assert board["board_return_20"] == pytest.approx(94 / 74 - 1)
    assert board["activity_score"] == 50
    assert board["market_share"] == 1
    assert board["coverage"] == {"expected": 6, "available": 6, "comparable": 6}
    calls = list(store.calls)
    store.ref = "b" * 64
    store.frames.clear()
    assert service.status()["view_id"] == view["view_id"]
    assert service.capital_view(view["view_id"])["boards"][0] == board
    history = service.history(view["view_id"], board["board_id"], window=60)
    assert len(history["points"]) == 60
    assert history["points"][-1]["prior20_mean_amount"] == 6 * 7450.
    assert service.members(view["view_id"], board["board_id"], offset=5)["rows"][0]["symbol"] == store.symbols[5]
    assert calls == store.calls


def test_same_two_day_members_and_metric_specific_coverage(tmp_path):
    store = FixtureStore(tmp_path)
    rows = store.frames["daily_bars"].to_dicts()
    rows = [row for row in rows if not (row["symbol"] == store.symbols[-1] and row["trade_date"] == store.dates[-1])]
    store.frames["daily_bars"] = pl.DataFrame(rows)
    view = MarketFeaturesService(store).prepare()
    board = view["boards"][0]
    assert board["amount"] == 5 * 8500.
    assert board["amount_change"] == 500.
    assert board["amount_change"] != 5 * 8500. - 6 * 8400.
    assert board["coverage"]["comparable"] == 5
    rows = [row for row in rows if not (row["symbol"] == store.symbols[-2] and row["trade_date"] == store.dates[-1])]
    store.frames["daily_bars"] = pl.DataFrame(rows)
    store.ref = "c" * 64
    board = MarketFeaturesService(store).prepare()["boards"][0]
    assert board["amount"] is None
    assert board["amount_change"] is None
    assert board["return_1"] is None


def test_missing_session_not_forward_filled_and_scope_exclusions(tmp_path):
    store = FixtureStore(tmp_path)
    members = store.frames["sector_members"].to_dicts()
    members.append({**members[0], "symbol": "920001.BJ"})
    members.append({**members[0], "symbol": "900001.SH"})
    store.frames["sector_members"] = pl.DataFrame(members)
    store.frames["daily_bars"] = store.frames["daily_bars"].filter(pl.col("trade_date") != store.dates[-2])
    service = MarketFeaturesService(store)
    view = service.prepare()
    board = view["boards"][0]
    assert board["coverage"]["expected"] == 6
    assert board["source_member_count"] == 8
    assert board["excluded_members"] == 2
    assert board["amount"] == 51000
    assert board["return_1"] is None
    assert board["relative_amount20"] is None
    assert board["amount_change"] is None
    assert service.history(view["view_id"], board["board_id"])["points"][-2]["amount"] is None


def test_top5_uses_full_population_not_amount_top30(tmp_path):
    store = FixtureStore(tmp_path)
    rows = store.frames["sector_bars"].to_dicts()
    store.frames["sector_bars"] = pl.DataFrame([{**row, "sector_code": str(board), "sector_name": f"Board {board}",
        "amount": 5000. - board * 100 if row["trade_date"] == store.dates[-1] else 4999. - board * 100 if board < 34 else 1.}
        for board in range(35) for row in rows])
    view = MarketFeaturesService(store).prepare(basis="source")
    service = MarketFeaturesService(store)
    narrow = service.capital_view(view["view_id"], limit=1)
    assert len(narrow["boards"]) == 1
    assert len(narrow["top_gainers"]) == 5
    assert narrow["filtered_boards"] == 35
    assert narrow["boards"][0]["code"] == "0"
    assert narrow["top_gainers"][0]["code"] == "34"


def test_source_units_and_no_name_member_mapping(tmp_path):
    store = FixtureStore(tmp_path)
    service = MarketFeaturesService(store)
    view = service.prepare(basis="source")
    board = view["boards"][0]
    assert view["units"]["amount"] == "source_native"
    assert board["amount"] == 85
    assert board["amount_change"] == 1
    assert board["market_share"] is None
    assert service.members(view["view_id"], board["board_id"])["mapping_status"] == "unsupported"
    assert service.members(view["view_id"], board["board_id"])["rows"] == []
    assert service.members(view["view_id"], board["board_id"])["status"] == "unsupported"


def test_corruption_and_invalid_arguments_fail_closed(tmp_path):
    service = MarketFeaturesService(FixtureStore(tmp_path))
    view = service.prepare()
    with pytest.raises(ValueError):
        service.capital_view(view["view_id"], limit=0)
    with pytest.raises(ValueError):
        service.history(view["view_id"], view["boards"][0]["board_id"], window=90)
    with pytest.raises(ValueError):
        service.capital_view("../latest")
    path = tmp_path / "var/market_views/views" / view["view_id"] / "view.json"
    path.write_bytes(path.read_bytes().replace(b'"observation_only"', b'"qualified"'))
    with pytest.raises(ValueError, match="identity"):
        service.status()


def test_end_bounds_prices_without_backdating_membership(tmp_path):
    store = FixtureStore(tmp_path)
    view = MarketFeaturesService(store).prepare(end=store.dates[-5].isoformat())
    assert view["price_date"] == store.dates[-5].isoformat()
    assert view["membership_date"] == store.dates[-1].isoformat()
    assert view["training_eligible"] is False


def test_duplicate_price_keys_are_missing_not_double_counted(tmp_path):
    store = FixtureStore(tmp_path)
    last = store.frames["daily_bars"].filter(pl.col("trade_date") == store.dates[-1])
    store.frames["daily_bars"] = pl.concat([store.frames["daily_bars"], last.head(2)])
    board = MarketFeaturesService(store).prepare()["boards"][0]
    assert board["amount"] is None
    assert board["coverage"]["available"] == 4


def test_activity_average_ties_and_absolute_negative_returns(tmp_path):
    from lake_equity.market.engine import _activity

    rows = [{"board_id": "a", "kind": "concept", "relative_amount20": 2., "return_1": -.1, "activity_score": None},
            {"board_id": "b", "kind": "concept", "relative_amount20": 2., "return_1": .1, "activity_score": None},
            {"board_id": "c", "kind": "concept", "relative_amount20": 1., "return_1": .01, "activity_score": None}]
    _activity(rows)
    assert [row["activity_score"] for row in rows] == [75., 75., 0.]


@pytest.mark.parametrize("basis", ["members", "source"])
@pytest.mark.parametrize("window", [20, 60])
def test_history_point_relative_amount_excludes_current_for_both_bases(tmp_path, basis, window):
    store = FixtureStore(tmp_path)
    service = MarketFeaturesService(store)
    view = service.prepare(basis=basis)
    points = service.history(view["view_id"], view["boards"][0]["board_id"], window=window)["points"]
    assert len(points) == window
    for point in points:
        position = store.dates.index(date.fromisoformat(point["date"]))
        assert point["relative_amount20"] == pytest.approx((position + 1) / (position - 9.5))
    assert points[-1]["relative_amount20"] == view["boards"][0]["relative_amount20"]


@pytest.mark.parametrize("basis", ["members", "source"])
@pytest.mark.parametrize("window", [20, 60])
def test_history_point_relative_amount_stays_null_across_missing_session(tmp_path, basis, window):
    store = FixtureStore(tmp_path)
    dataset = "daily_bars" if basis == "members" else "sector_bars"
    store.frames[dataset] = store.frames[dataset].filter(pl.col("trade_date") != store.dates[-3])
    service = MarketFeaturesService(store)
    view = service.prepare(basis=basis)
    points = service.history(view["view_id"], view["boards"][0]["board_id"], window=window)["points"]
    assert len(points) == window
    assert points[-3]["amount"] is None
    assert all(point["relative_amount20"] is None for point in points[-3:])


@pytest.mark.parametrize("symbol", ["689009.SH", "399001.SZ", "610001.SH", "690001.SH", "900901.SH",
    "200001.SZ", "515000.SH", "159001.SZ", "920001.BJ", "830001.BJ", "600000.SZ", "000001.SH"])
def test_common_stock_scope_excludes_cdr_index_funds_and_non_a_segments(symbol):
    from lake_equity.market.engine import _supported

    assert _supported(symbol) is False


@pytest.mark.parametrize("symbol", ["600000.SH", "601001.SH", "688001.SH", "000001.SZ", "003001.SZ", "300001.SZ", "301001.SZ"])
def test_common_stock_scope_uses_cne_equity_whitelist(symbol):
    from lake_equity.market.engine import _supported

    assert _supported(symbol) is True


def test_excluded_cdr_and_index_do_not_enter_board_or_market_denominator(tmp_path):
    store = FixtureStore(tmp_path)
    members = store.frames["sector_members"].to_dicts()
    prices = store.frames["daily_bars"].to_dicts()
    for symbol in ("689009.SH", "399001.SZ", "610001.SH", "690001.SH"):
        members.append({**members[0], "symbol": symbol})
        prices.append({**prices[-1], "symbol": symbol, "amount": 1e12})
    store.frames["sector_members"] = pl.DataFrame(members)
    store.frames["daily_bars"] = pl.DataFrame(prices)
    view = MarketFeaturesService(store).prepare()
    board = view["boards"][0]
    assert board["source_member_count"] == 10
    assert board["excluded_members"] == 4
    assert board["coverage"]["expected"] == 6
    assert board["amount"] == 51000.
    assert view["summary"]["market_amount"] == 51000.
    assert view["summary"]["observed_market_members"] == 6
