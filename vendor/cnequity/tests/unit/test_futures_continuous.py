"""Continuous futures: rolls decided on T-1, forward only, factors continuous."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from cnequity.derive.futures_continuous import compute_futures_continuous

D = [date(2026, 3, 2) + timedelta(days=i) for i in range(5)]


def _bars(table: dict[str, list[tuple[float, int]]]) -> pl.DataFrame:
    rows = []
    for symbol, points in table.items():
        for day, (price, oi) in zip(D, points, strict=True):
            rows.append(
                {
                    "symbol": symbol,
                    "exchange": "SHF",
                    "product": "CU",
                    "trade_date": day,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "settle": price,
                    "volume": 10,
                    "open_interest": oi,
                }
            )
    return pl.DataFrame(rows)


def _main(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.filter(pl.col("series") == "main").sort("trade_date")


def test_the_roll_uses_the_previous_close_not_the_same_session():
    bars = _bars(
        {
            "CU2605.SHF": [(100, 50), (100, 50), (100, 50), (100, 50), (100, 50)],
            # Out-holds the front from D[2]'s close, so it is main from D[3].
            "CU2606.SHF": [(110, 10), (110, 10), (110, 90), (110, 90), (110, 90)],
        }
    )
    main = _main(compute_futures_continuous(bars))
    by_day = dict(zip(main["trade_date"], main["contract_symbol"], strict=True))
    assert by_day[D[2]] == "CU2605.SHF"
    assert by_day[D[3]] == "CU2606.SHF"
    assert main.filter(pl.col("trade_date") == D[3])["rolled"].item() is True


def test_adjusted_prices_are_continuous_across_the_roll():
    bars = _bars(
        {
            "CU2605.SHF": [(100, 50)] * 5,
            "CU2606.SHF": [(110, 10), (110, 10), (110, 90), (110, 90), (110, 90)],
        }
    )
    main = _main(compute_futures_continuous(bars))
    before = main.filter(pl.col("trade_date") == D[2]).row(0, named=True)
    after = main.filter(pl.col("trade_date") == D[3]).row(0, named=True)
    assert after["settle"] * after["adj_ratio"] == before["settle"] * before["adj_ratio"]
    assert after["settle"] + after["adj_diff"] == before["settle"] + before["adj_diff"]


def test_rolls_never_go_back_to_an_earlier_month():
    bars = _bars(
        {
            "CU2605.SHF": [(100, 10), (100, 10), (100, 99), (100, 99), (100, 99)],
            "CU2606.SHF": [(110, 50)] * 5,
        }
    )
    main = _main(compute_futures_continuous(bars))
    assert set(main["contract_symbol"]) == {"CU2606.SHF"}


def test_a_contract_about_to_expire_hands_over_even_with_more_interest():
    bars = _bars({"CU2603.SHF": [(100, 90)] * 5, "CU2604.SHF": [(105, 10)] * 5})
    main = _main(
        compute_futures_continuous(
            bars, ends={"CU2603.SHF": date(2026, 3, 16), "CU2604.SHF": date(2026, 4, 15)}
        )
    )
    # Five calendar days before 2026-03-16 is 03-11: still eligible on D[4]=03-06.
    assert set(main["contract_symbol"]) == {"CU2603.SHF"}
    main = _main(compute_futures_continuous(bars, ends={"CU2603.SHF": date(2026, 3, 8)}))
    assert main.filter(pl.col("trade_date") == D[4])["contract_symbol"].item() == "CU2604.SHF"


def test_roll_yield_is_positive_in_backwardation():
    bars = _bars({"CU2605.SHF": [(110, 90)] * 5, "CU2606.SHF": [(100, 10)] * 5})
    main = _main(compute_futures_continuous(bars))
    assert (main["roll_yield"] > 0).all()


def test_second_is_absent_when_main_rolls_to_last_available_month():
    bars = _bars(
        {
            "CU2605.SHF": [(100, 90), (100, 90), (100, 10), (100, 10), (100, 10)],
            "CU2606.SHF": [(110, 10), (110, 10), (110, 99), (110, 99), (110, 99)],
        }
    )
    frame = compute_futures_continuous(bars)
    assert frame.filter((pl.col("trade_date") == D[3]) & (pl.col("series") == "second")).is_empty()
    assert frame.filter(pl.col("trade_date") == D[3])["contract_symbol"].to_list() == ["CU2606.SHF"]


def test_missing_main_does_not_allow_rollback_and_missing_roll_price_invalidates_factors():
    bars = _bars({"CU2605.SHF": [(100, 90)] * 5, "CU2606.SHF": [(110, 99)] * 5})
    bars = bars.filter(~((pl.col("symbol") == "CU2606.SHF") & (pl.col("trade_date") == D[2])))
    main = _main(compute_futures_continuous(bars))
    assert main.filter(pl.col("trade_date") == D[3]).is_empty()
    assert set(main["contract_symbol"]) == {"CU2606.SHF"}
    farther = _bars({"CU2607.SHF": [(120, 1), (120, 1), (120, 200), (120, 200), (120, 200)]})
    main = _main(compute_futures_continuous(pl.concat([bars, farther])))
    row = main.filter(pl.col("trade_date") == D[3]).row(0, named=True)
    assert row["contract_symbol"] == "CU2607.SHF"
    assert row["adj_ratio"] is None and row["adj_diff"] is None


def test_missing_trading_session_is_not_used_as_yesterdays_close():
    bars = _bars({"CU2605.SHF": [(100, 90)] * 5})
    bars = bars.filter(pl.col("trade_date") != D[2])
    main = _main(compute_futures_continuous(bars))
    assert D[3] not in main["trade_date"]
    assert main.filter(pl.col("trade_date") == D[4])["adj_ratio"].item() is None


def test_observed_futures_session_missing_from_shared_calendar_keeps_next_day():
    bars = _bars({"CU2605.SHF": [(100, 90)] * 5})
    calendar = [D[0], D[1], D[3], D[4]]
    main = _main(compute_futures_continuous(bars, sessions=calendar))
    assert D[2] in main["trade_date"]
    assert D[3] in main["trade_date"]


def test_other_exchange_special_session_does_not_hide_a_gap():
    shf = _bars({"CU2605.SHF": [(100, 90)] * 5}).filter(pl.col("trade_date") != D[2])
    dce = _bars({"CU2605.SHF": [(100, 90)] * 5}).with_columns(
        pl.lit("DCE").alias("exchange"),
        pl.lit("M").alias("product"),
        pl.lit("M2605.DCE").alias("symbol"),
    )
    bars = pl.concat([shf, dce])
    main = _main(compute_futures_continuous(bars, sessions=D))
    assert main.filter((pl.col("symbol") == "CU.SHF") & (pl.col("trade_date") == D[3])).is_empty()
