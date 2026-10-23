from datetime import date, datetime, timezone

import polars as pl
import pytest

from cnequity.config import Config
from cnequity.query import load
from cnequity.query.reader import ReaderError

FETCHED = datetime(2026, 9, 29, tzinfo=timezone.utc)
DAYS = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9)]


def _write(path, rows, **overrides):
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema_overrides=overrides or None).write_parquet(path)


@pytest.fixture
def lake(tmp_path):
    cfg = Config(data_root=tmp_path)
    # A fund paying 0.1 on day 2 (price drops from 2.0 to 1.9), and a stock
    # whose hfq factor already steps on its own dividend.
    closes = {"510300.SH": [2.0, 2.0, 1.9, 1.95], "600000.SH": [10.0, 10.0, 9.5, 9.5]}
    hfq = {"510300.SH": [1.0, 1.0, 1.0, 1.0], "600000.SH": [1.0, 1.0, 1.0 / 0.95, 1.0 / 0.95]}
    for index, day in enumerate(DAYS):
        _write(
            cfg.curated_root / f"daily_bars/trade_date={day}/part.parquet",
            [
                {
                    "symbol": s,
                    "trade_date": day,
                    "open": c[index],
                    "high": c[index],
                    "low": c[index],
                    "close": c[index],
                    "volume": 100,
                    "amount": 100 * c[index],
                    "source": "tdx_protocol",
                    "data_version": "v2",
                    "fetched_at": FETCHED,
                }
                for s, c in closes.items()
            ],
        )
        _write(
            cfg.derived_root / f"adj_factors/trade_date={day}/part-0.parquet",
            [
                {
                    "symbol": s,
                    "trade_date": day,
                    "adjust_type": "hfq",
                    "factor": f[index],
                    "source": "sina",
                    "data_version": "v1",
                    "fetched_at": FETCHED,
                }
                for s, f in hfq.items()
            ],
        )
    _write(
        cfg.curated_root / "corporate_actions/ex_date=2025/part.parquet",
        [
            {
                "symbol": s,
                "ex_date": DAYS[2],
                "action_type": "cash_dividend",
                "cash_dividend": cash,
                "bonus_ratio": 0.0,
                "transfer_ratio": 0.0,
                "allotment_ratio": None,
                "allotment_price": None,
                "split_factor": 1.0,
                "source": "tdx_protocol",
                "data_version": "v1",
                "fetched_at": FETCHED,
            }
            for s, cash in (("510300.SH", 0.1), ("600000.SH", 0.5))
        ],
        allotment_ratio=pl.Float64,
        allotment_price=pl.Float64,
    )
    _write(
        cfg.curated_root / "instruments/part-merged.parquet",
        [
            {
                "symbol": s,
                "name": s,
                "exchange": "SH",
                "asset_type": kind,
                "list_date": date(2012, 5, 28),
                "delist_date": None,
                "prev_symbol": None,
                "source": "tdx_protocol",
                "data_version": "v1",
                "fetched_at": FETCHED,
            }
            for s, kind in (("510300.SH", "etf"), ("600000.SH", "stock"))
        ],
        delist_date=pl.Date,
        prev_symbol=pl.Utf8,
    )
    return cfg


def _closes(cfg, adjust, symbol, **kwargs):
    frame = load("daily_bars", config=cfg, adjust=adjust, symbols=[symbol], **kwargs)
    return frame.sort("trade_date")["adj_close"].to_list()


def test_fund_distributions_are_reinvested_only_in_total_return(lake):
    # hfq keeps its meaning: the fund's split-only factor shows the payout drop.
    assert _closes(lake, "hfq", "510300.SH") == pytest.approx([2.0, 2.0, 1.9, 1.95])
    total = _closes(lake, "total_return", "510300.SH")
    assert total == pytest.approx([2.0, 2.0, 2.0, 1.95 * 2.0 / 1.9])


def test_a_stock_reads_the_same_as_hfq(lake):
    assert _closes(lake, "total_return", "600000.SH") == pytest.approx(
        _closes(lake, "hfq", "600000.SH")
    )


def test_level_is_anchored_on_the_first_bar_in_scope(lake):
    later = _closes(lake, "total_return", "510300.SH", start=str(DAYS[2]))
    # The payout before the scope starts is not applied: returns stay exact.
    assert later == pytest.approx([1.9, 1.95])


def test_total_return_is_for_daily_bars_only(lake):
    with pytest.raises(ReaderError, match="daily_bars only"):
        load("minute_bars", config=lake, adjust="total_return")


def test_a_total_return_receipt_pins_the_distribution_inputs(tmp_path):
    from cnequity.config import Config
    from cnequity.query.receipt import _dependencies

    deps = _dependencies("daily_bars", {"adjust": "total_return"}, Config(data_root=tmp_path))
    assert {"adj_factors", "corporate_actions", "instruments"} <= deps


def test_sql_macro_matches_the_python_total_return(lake):
    import duckdb

    from cnequity.query.views import ensure_duckdb_views

    db = ensure_duckdb_views(lake)
    with duckdb.connect(str(db), read_only=True) as con:
        rows = con.execute(
            "SELECT tr_close FROM daily_bars_total_return(NULL, NULL) "
            "WHERE symbol = '510300.SH' ORDER BY trade_date"
        ).fetchall()
    assert [row[0] for row in rows] == pytest.approx(_closes(lake, "total_return", "510300.SH"))


def test_halted_fund_payout_lands_on_resume_in_python_and_sql(lake, halted_ex_event):
    import duckdb

    from cnequity.query.views import ensure_duckdb_views

    halt = halted_ex_event("510300.SH", DAYS[1], DAYS[2], DAYS[3])
    bar_path = lake.curated_root / f"daily_bars/trade_date={halt['ex_date']}/part.parquet"
    pl.read_parquet(bar_path).filter(pl.col("symbol") != halt["symbol"]).write_parquet(bar_path)

    expected = [2.0, 2.0, 1.95 * 2.0 / 1.9]
    assert _closes(lake, "total_return", halt["symbol"]) == pytest.approx(expected)
    db = ensure_duckdb_views(lake)
    with duckdb.connect(str(db), read_only=True) as con:
        rows = con.execute(
            "SELECT tr_close FROM daily_bars_total_return(NULL, NULL) "
            "WHERE symbol = '510300.SH' ORDER BY trade_date"
        ).fetchall()
    assert [row[0] for row in rows] == pytest.approx(expected)
