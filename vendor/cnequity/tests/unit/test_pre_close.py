"""``pre_close``: an optional exchange fact that a K-line refresh cannot erase."""

from __future__ import annotations

from datetime import date, datetime

import polars as pl

from cnequity.domain.canonical import dedupe_by_primary_key, dedupe_lazy_by_primary_key
from cnequity.domain.schemas import validate_dataframe, with_provenance

_D = date(2026, 9, 30)


def _bar(source: str, fetched: datetime, pre_close: float | None) -> pl.DataFrame:
    frame = pl.DataFrame(
        {
            "symbol": ["600519.SH"],
            "trade_date": [_D],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "pre_close": [pre_close],
            "volume": [100],
            "amount": [1000.0],
        },
        schema_overrides={"pre_close": pl.Float64},
    )
    return with_provenance(frame, source=source, data_version="v2").with_columns(
        pl.lit(fetched).cast(pl.Datetime("us", "UTC")).alias("fetched_at")
    )


def test_a_writer_without_pre_close_validates_with_null():
    frame = _bar("tdx_protocol", datetime(2026, 9, 30, 16), None).drop("pre_close")
    out = validate_dataframe(frame, "daily_bars")
    assert out["pre_close"].to_list() == [None]


def test_refresh_keeps_the_exchange_pre_close():
    quote = _bar("tdx_protocol_quote", datetime(2026, 9, 30, 15, 5), 10.2)
    sweep = _bar("tdx_protocol", datetime(2026, 10, 6, 18), None)
    both = pl.concat([quote, sweep])

    out = dedupe_by_primary_key(both, "daily_bars")
    lazy = dedupe_lazy_by_primary_key(both.lazy(), "daily_bars").collect()

    for frame in (out, lazy):
        assert frame.height == 1
        assert frame["pre_close"].item() == 10.2


def test_a_rows_own_pre_close_is_not_rewritten():
    older = _bar("tdx_protocol_quote", datetime(2026, 9, 30, 15, 5), 10.2)
    newer = _bar("exchange", datetime(2026, 9, 30, 16), 10.25)

    out = dedupe_by_primary_key(pl.concat([older, newer]), "daily_bars")
    own = {"tdx_protocol_quote": 10.2, "exchange": 10.25}
    assert out["pre_close"].item() == own[out["source"].item()]
