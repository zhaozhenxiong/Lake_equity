from datetime import date, datetime, timezone

import polars as pl

from cnequity.config import Config
from cnequity.storage.instrument_catalog import load_curated_trading_status

FETCHED = datetime(2026, 9, 30, tzinfo=timezone.utc)
DAY = date(2026, 8, 7)


def _row(symbol, status, source):
    return {
        "symbol": symbol,
        "trade_date": DAY,
        "is_trading": status == "normal",
        "status": status,
        "risk_warning": None,
        "source": source,
        "data_version": "v1",
        "fetched_at": FETCHED,
    }


def test_a_suspension_inferred_from_missing_bars_is_not_evidence(tmp_path):
    cfg = Config(data_root=tmp_path)
    part = cfg.curated_root / "trading_status" / f"trade_date={DAY}"
    part.mkdir(parents=True)
    pl.DataFrame(
        [
            # The failed run's hole, labelled a suspension after the fact.
            _row("920001.BJ", "suspended", "derived_bar_gap"),
            # An independent board read on the same key outranked by it.
            _row("920002.BJ", "suspended", "derived_bar_gap"),
            _row("920002.BJ", "normal", "eastmoney_cached"),
            _row("920003.BJ", "suspended", "bse"),
        ],
        schema_overrides={"risk_warning": pl.Boolean},
    ).write_parquet(part / "part-0.parquet")

    status = load_curated_trading_status(cfg).sort("symbol")
    assert status.select("symbol", "status", "source").rows() == [
        ("920002.BJ", "normal", "eastmoney_cached"),
        ("920003.BJ", "suspended", "bse"),
    ]
    inferred = load_curated_trading_status(cfg, include_inferred=True)
    assert "920001.BJ" in inferred.get_column("symbol").to_list()
