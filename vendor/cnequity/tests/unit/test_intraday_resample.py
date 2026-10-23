from datetime import date, datetime, timedelta

import polars as pl
import pytest

from cnequity.query import resample_minute_history, resample_trade_bars


def bars(start=datetime(2026, 9, 15, 9, 31), count=5, step=1):
    return pl.DataFrame(
        {
            "symbol": ["603869.SH"] * count,
            "trade_date": [date(2026, 9, 15)] * count,
            "bar_time": [start + timedelta(minutes=i * step) for i in range(count)],
            "frequency": [f"{step}m"] * count,
            **{k: [10.0] * count for k in ["open", "high", "low", "close"]},
            "volume": [100] * count,
            "amount": [1000.0] * count,
        }
    )


def test_untraded_opening_carry_does_not_change_trade_ohlc():
    frame = bars().with_columns(
        pl.Series("volume", [0, 100, 0, 100, 0]),
        pl.Series("amount", [0.0, 1000.0, 0.0, 1000.0, 0.0]),
        *[pl.Series(k, [20.0, 10.0, 20.0, 10.0, 20.0]) for k in ["open", "high", "low", "close"]],
    )
    result = resample_trade_bars(frame)
    assert result.select("open", "high", "low", "close").row(0) == (10.0, 10.0, 10.0, 10.0)
    assert result.select("volume", "amount").row(0) == (200, 2000.0)
    assert result["bar_time"][0] == datetime(2026, 9, 15, 9, 35)


def test_positive_amount_retains_trade_when_volume_rounded_to_zero():
    frame = bars().with_columns(
        pl.lit(0).alias("volume"),
        pl.Series("amount", [100.0, 0.0, 0.0, 0.0, 0.0]),
        *[pl.Series(k, [10.0, 20.0, 20.0, 20.0, 20.0]) for k in ["open", "high", "low", "close"]],
    )
    result = resample_trade_bars(frame)
    assert result["amount"][0] == 100
    assert result.select("open", "high", "low", "close").row(0) == (10.0, 10.0, 10.0, 10.0)


def test_all_untraded_interval_keeps_quote_and_zero_quantities():
    result = resample_trade_bars(
        bars().with_columns(pl.lit(0).alias("volume"), pl.lit(0.0).alias("amount"))
    )
    assert result.select("open", "high", "low", "close", "volume", "amount").row(0) == (
        10.0,
        10.0,
        10.0,
        10.0,
        0,
        0.0,
    )


def test_hourly_intervals_anchor_to_each_half_session():
    frame = pl.concat([bars(count=120), bars(datetime(2026, 9, 15, 13, 1), 120)])
    result = resample_trade_bars(frame, "60m")
    assert [(d.hour, d.minute) for d in result["bar_time"]] == [
        (10, 30),
        (11, 30),
        (14, 0),
        (15, 0),
    ]
    assert result["volume"].sum() == 24000


def test_missing_and_duplicate_minutes_are_not_silently_filled():
    with pytest.raises(ValueError, match="incomplete"):
        resample_trade_bars(bars().slice(1))
    with pytest.raises(ValueError, match="duplicate"):
        resample_trade_bars(pl.concat([bars(), bars().head(1)]))


def test_lunch_and_mismatched_dates_are_rejected():
    with pytest.raises(ValueError, match="session"):
        resample_trade_bars(bars(datetime(2026, 9, 15, 12, 1)))
    with pytest.raises(ValueError, match="trade date"):
        resample_trade_bars(bars().with_columns(pl.lit(date(2026, 9, 14)).alias("trade_date")))


def five_minute(start=datetime(2026, 9, 15, 9, 35), count=3):
    return bars(start, count, step=5)


def test_five_minute_bars_build_session_anchored_coarser_bars():
    frame = pl.concat([five_minute(count=24), five_minute(datetime(2026, 9, 15, 13, 5), 24)])
    hourly = resample_trade_bars(frame, "60m")
    assert [(d.hour, d.minute) for d in hourly["bar_time"]] == [
        (10, 30),
        (11, 30),
        (14, 0),
        (15, 0),
    ]
    assert hourly["volume"].to_list() == [1200] * 4
    assert set(hourly["frequency"]) == {"60m"}
    quarter = resample_trade_bars(five_minute(), "15m")
    assert quarter["bar_time"].to_list() == [datetime(2026, 9, 15, 9, 45)]


def test_untraded_five_minute_bar_does_not_set_trade_ohlc():
    frame = five_minute().with_columns(
        pl.Series("volume", [0, 100, 100]),
        pl.Series("amount", [0.0, 1000.0, 1000.0]),
        *[pl.Series(k, [20.0, 10.0, 11.0]) for k in ["open", "high", "low", "close"]],
    )
    result = resample_trade_bars(frame, "15m")
    assert result.select("open", "high", "low", "close").row(0) == (10.0, 11.0, 10.0, 11.0)


def test_five_minute_input_rejects_finer_targets_mixes_and_gaps():
    with pytest.raises(ValueError, match="5m bars resample only to 15m, 30m, 60m"):
        resample_trade_bars(five_minute(), "5m")
    with pytest.raises(ValueError, match="exclusively"):
        resample_trade_bars(
            pl.concat([bars(), five_minute(datetime(2026, 9, 15, 9, 40), 2)]), "15m"
        )
    with pytest.raises(ValueError, match="incomplete.*5m bar"):
        resample_trade_bars(five_minute().slice(1), "15m")


def test_five_minute_timestamps_must_sit_on_the_session_grid():
    with pytest.raises(ValueError, match="session"):
        resample_trade_bars(five_minute(datetime(2026, 9, 15, 9, 33)), "15m")
    with pytest.raises(ValueError, match="session"):
        resample_trade_bars(five_minute(datetime(2026, 9, 15, 9, 30)), "15m")


def on_day(frame, day):
    return frame.with_columns(
        pl.lit(day).alias("trade_date"),
        pl.col("bar_time").dt.replace(day=day.day),
    )


def test_history_uses_one_minute_days_and_five_minute_for_the_rest():
    older, newer = date(2026, 9, 14), date(2026, 9, 15)
    minute_1m = bars(count=15).with_columns(pl.lit(7).alias("volume"))
    minute_5m = pl.concat([on_day(five_minute(), older), five_minute()])
    result = resample_minute_history(minute_1m, minute_5m, "15m")
    assert result.select("trade_date", "resampled_from", "volume").rows() == [
        (older, "5m", 300),
        (newer, "1m", 105),
    ]
    assert set(result["frequency"]) == {"15m"}


def test_history_is_per_symbol_and_keeps_one_minute_failures():
    minute_5m = pl.concat(
        [five_minute(), five_minute().with_columns(pl.lit("600000.SH").alias("symbol"))]
    )
    result = resample_minute_history(bars(count=15), minute_5m, "15m")
    assert result.select("symbol", "resampled_from").rows() == [
        ("600000.SH", "5m"),
        ("603869.SH", "1m"),
    ]
    with pytest.raises(ValueError, match="incomplete"):
        resample_minute_history(bars(count=14), five_minute(), "15m")


def test_history_rejects_swapped_inputs_and_sub_five_minute_targets():
    with pytest.raises(ValueError, match="minute_1m must contain only 1m"):
        resample_minute_history(five_minute(), five_minute(), "15m")
    with pytest.raises(ValueError, match="minute_5m must contain only 5m"):
        resample_minute_history(bars(count=15), bars(count=15), "15m")
    with pytest.raises(ValueError, match="15m, 30m or 60m"):
        resample_minute_history(bars(), five_minute(), "5m")


def test_history_with_empty_one_minute_input_falls_back_entirely():
    result = resample_minute_history(bars().clear(), five_minute(), "15m")
    assert result["resampled_from"].to_list() == ["5m"]
