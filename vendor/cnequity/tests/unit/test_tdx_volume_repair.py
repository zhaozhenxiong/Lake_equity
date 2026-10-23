"""Rewriting the volumes the pre-09-17 TDX decoder inflated.

``get_volume`` turned every value under 64.5 lots into a large wrong one (4 lots
became 8,194). The repair re-reads TDX and replaces only the volume of stored
TDX rows that the fresh read otherwise matches exactly; it never adds a key.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from cnequity.config import Config
from cnequity.steps import bars

DAY = date(2016, 1, 6)


def _row(**over) -> dict:
    row = {
        "symbol": "160618.SZ",
        "trade_date": DAY,
        "open": 1.0,
        "high": 1.01,
        "low": 0.99,
        "close": 1.0,
        "volume": 819_400.0,
        "amount": 400.0,
        "source": "tdx_protocol",
    }
    row.update(over)
    return row


def _fresh(**over) -> pl.DataFrame:
    row = _row(volume=400.0, **over)
    row.pop("source")
    return pl.DataFrame([row])


def test_a_decode_artefact_takes_the_fresh_volume():
    corrected, counts = bars._tdx_volume_corrections(pl.DataFrame([_row()]), _fresh())

    assert corrected["volume"].to_list() == [400.0]
    assert corrected.columns == list(_row())
    assert counts["rows_corrected"] == 1


def test_a_row_the_fresh_read_already_agrees_with_is_left_alone():
    corrected, counts = bars._tdx_volume_corrections(pl.DataFrame([_row(volume=400.0)]), _fresh())

    assert corrected.is_empty()
    assert counts == {
        "rows_checked": 1,
        "rows_unserved": 0,
        "rows_disagreeing": 0,
        "rows_corrected": 0,
        "amounts_corrected": 0,
    }


@pytest.mark.parametrize("over", [{"close": 1.01}, {"amount": 800.0}])
def test_a_different_session_is_counted_not_rewritten(over):
    corrected, counts = bars._tdx_volume_corrections(pl.DataFrame([_row()]), _fresh(**over))

    assert corrected.is_empty()
    assert counts["rows_disagreeing"] == 1


def test_a_turnover_in_the_inflated_range_is_rewritten_with_the_volume():
    """The decoder read turnover the same way: 1 lot at 0.63 is 63 yuan, and
    the stored row said 528 beside 32,768 lots."""
    stored = pl.DataFrame([_row(close=0.63, open=0.63, volume=3_276_800.0, amount=528.0)])
    fresh = _fresh(close=0.63, open=0.63).with_columns(
        pl.lit(100.0).alias("volume"), pl.lit(63.0).alias("amount")
    )
    corrected, counts = bars._tdx_volume_corrections(stored, fresh)

    assert corrected.select("volume", "amount").rows() == [(100.0, 63.0)]
    assert counts["amounts_corrected"] == 1


def test_a_key_tdx_no_longer_serves_is_counted():
    corrected, counts = bars._tdx_volume_corrections(
        pl.DataFrame([_row()]), _fresh().with_columns(pl.lit(date(2016, 1, 7)).alias("trade_date"))
    )

    assert corrected.is_empty()
    assert counts["rows_unserved"] == 1


def test_the_repair_writes_only_corrected_tdx_rows(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "lake")
    stored = pl.DataFrame(
        [
            _row(),
            _row(symbol="160619.SZ", volume=400.0),
            _row(symbol="160620.SZ", source="eastmoney"),
        ]
    )
    monkeypatch.setattr(bars, "_resolve_daily_bar_scope", lambda config, symbols: symbols)
    monkeypatch.setattr(
        "cnequity.query.parquet_scan.collect_parquet_root",
        lambda root, **k: stored.filter(pl.col("symbol").is_in(k["symbols"])),
    )
    monkeypatch.setattr(
        "cnequity.adapters.tdx_protocol.client.fetch_daily_bars",
        lambda symbols, start, end, **k: pl.concat(
            [_fresh(symbol=s) for s in symbols], how="vertical"
        ),
    )
    written: list[pl.DataFrame] = []
    monkeypatch.setattr(
        "cnequity.steps.http_common.write_fetched",
        lambda config, run_id, dataset, df, **k: written.append(df) or {"rows_written": df.height},
    )

    result = bars.repair_tdx_volumes(
        cfg, DAY, DAY, "run-1", ["160618.SZ", "160619.SZ", "160620.SZ"]
    )

    assert [df.select("symbol", "volume").rows() for df in written] == [[("160618.SZ", 400.0)]]
    assert result["rows_written"] == 1
    assert "status" not in result


def test_the_repair_refuses_an_unscoped_run(tmp_path):
    with pytest.raises(RuntimeError, match="--symbols"):
        bars.repair_tdx_volumes(Config(data_root=tmp_path / "lake"), DAY, DAY, "run-1", None)


@pytest.mark.parametrize(
    ("stored_volume", "fresh_volume"), [(437.0, 400.0), (2_744_507_800.0, 2_744_508_000.0)]
)
def test_an_exact_odd_lot_volume_is_finer_not_wrong(stored_volume, fresh_volume):
    """TDX history counts whole lots; a stored 437 against a fresh 400 is the
    better figure, and a two-lot gap on 27 million lots is rounding."""
    fresh = _fresh().with_columns(pl.lit(fresh_volume).alias("volume"))
    corrected, counts = bars._tdx_volume_corrections(
        pl.DataFrame([_row(volume=stored_volume)]), fresh
    )

    assert corrected.is_empty()
    assert counts["rows_corrected"] == 0
