"""SHFE/INE, CZCE and GFEX daily files across their format eras."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from cnequity.adapters.futures_exchange import czce, gfex, shfe
from cnequity.adapters.futures_exchange.common import (
    FuturesDayUnavailable,
    FuturesPayloadError,
    looks_like_challenge,
)
from cnequity.domain.schemas import validate_dataframe, with_provenance

FIXTURES = Path(__file__).parents[1] / "fixtures" / "futures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _valid(frame: pl.DataFrame, dataset: str) -> None:
    stamped = with_provenance(
        frame.with_columns(pl.lit("futures_exchange").alias("source")),
        source="futures_exchange",
        data_version="v1",
    )
    assert validate_dataframe(stamped, dataset).height == frame.height


# --- SHFE / INE ---------------------------------------------------------------


def test_shfe_2002_file_has_padded_codes_and_no_turnover():
    frame = shfe.parse_futures(_read("shfe_20020107.json"), date(2002, 1, 7))
    assert frame["symbol"].str.starts_with("CU").all()
    assert "CU0201.SHF" in frame["symbol"].to_list()
    assert frame["amount"].null_count() == frame.height
    _valid(frame, "futures_bars")


def test_shfe_before_2020_is_halved_and_ine_is_split_out():
    frame = shfe.parse_futures(_read("shfe_20191231.json"), date(2019, 12, 31))
    assert set(frame["exchange"]) == {"SHF", "INE"}
    assert frame.filter(pl.col("product") == "SC")["exchange"].unique().to_list() == ["INE"]
    raw = json.loads(_read("shfe_20191231.json"))["o_curinstrument"]
    rb2001 = next(
        r for r in raw if r["PRODUCTID"].strip() == "rb_f" and r["DELIVERYMONTH"] == "2001"
    )
    row = frame.filter(pl.col("symbol") == "RB2001.SHF").row(0, named=True)
    assert row["volume"] * 2 == rb2001["VOLUME"]
    assert row["open_interest"] * 2 == rb2001["OPENINTEREST"]


def test_shfe_2018_reads_ine_from_its_own_official_file(monkeypatch):
    day = date(2018, 3, 26)
    urls = []

    def fetch(url, **_kwargs):
        urls.append(url)
        return url.encode()

    def parse(body, _day):
        url = body.decode()
        if "www.ine.cn" in url:
            return pl.DataFrame({"symbol": ["SC1809.INE"], "exchange": ["INE"]})
        return pl.DataFrame({"symbol": ["CU1804.SHF", "SC1809.INE"], "exchange": ["SHF", "INE"]})

    monkeypatch.setattr(shfe, "fetch_bytes", fetch)
    monkeypatch.setattr(shfe, "parse_futures", parse)
    frame = shfe.fetch_shfe_day(day, kind="futures").futures
    assert frame["symbol"].to_list() == ["CU1804.SHF", "SC1809.INE"]
    assert urls == [
        shfe.FUTURES_URL.format(ymd="20180326"),
        shfe.INE_FUTURES_URL.format(ymd="20180326"),
    ]
    urls.clear()
    shfe.fetch_shfe_day(date(2019, 1, 2), kind="futures")
    assert urls == [shfe.FUTURES_URL.format(ymd="20190102")]


def test_shfe_current_file_skips_tas_and_efp_rows():
    frame = shfe.parse_futures(_read("shfe_20260924.json"), date(2026, 9, 24))
    assert not frame["exchange_code"].str.contains("tas|efp").any()
    cu = frame.filter(pl.col("product") == "CU")
    assert cu["amount"].null_count() == 0
    _valid(frame, "futures_bars")


def test_shfe_subtotal_mismatch_is_refused():
    payload = json.loads(_read("shfe_20260924.json"))
    for row in payload["o_curinstrument"]:
        if row["PRODUCTID"].strip() == "cu_f" and row["DELIVERYMONTH"] == "小计":
            row["VOLUME"] = int(row["VOLUME"]) + 1
    with pytest.raises(FuturesPayloadError, match="小计"):
        shfe.parse_futures(json.dumps(payload).encode(), date(2026, 9, 24))


def test_shfe_options_carry_delta_exercises_and_series_volatility():
    frame = shfe.parse_options(_read("shfe_option_20260924.json"), date(2026, 9, 24))
    assert frame["product"].unique().to_list() == ["OP"]
    assert frame["delta"].null_count() == 0
    assert set(frame["option_type"]) == {"C", "P"}
    series = frame["underlying_symbol"].unique().to_list()
    assert all(s.startswith("OP") and s.endswith(".SHF") for s in series)
    _valid(frame, "option_bars")


def test_shfe_holiday_page_is_no_file():
    with pytest.raises(FuturesDayUnavailable):
        shfe.parse_futures(_read("shfe_holiday_404.html"), date(2026, 9, 25))


# --- CZCE -------------------------------------------------------------------


def test_czce_comma_archive_keeps_renamed_products():
    frame = czce.parse_futures(_read("czce_20120104.txt"), date(2012, 1, 4))
    assert set(frame["product"]) == {"ME", "WS"}
    # One-digit years resolved against the 2012 session they were quoted in.
    assert frame["symbol"].str.contains(r"^(ME|WS)1[23]\d\d\.CZC$").all()
    _valid(frame, "futures_bars")


def test_czce_2019_counts_are_halved():
    frame = czce.parse_futures(_read("czce_20191231.txt"), date(2019, 12, 31))
    ma004 = frame.filter(pl.col("symbol") == "MA2004.CZC").row(0, named=True)
    # The file says 48; one side of it is 24 (and is 24 on 2020-01-02).
    assert ma004["open_interest"] == 24


def test_czce_current_file_nulls_prices_of_untraded_contracts():
    frame = czce.parse_futures(_read("czce_20260924.txt"), date(2026, 9, 24))
    idle = frame.filter(pl.col("volume") == 0)
    assert idle["close"].null_count() == idle.height
    assert idle["settle"].null_count() == 0
    _valid(frame, "futures_bars")


def test_czce_options_parse_percent_iv_and_the_second_series():
    frame = czce.parse_options(_read("czce_option_20260924.txt"), date(2026, 9, 24))
    assert frame["implied_vol"].drop_nulls().max() < 5
    tagged = frame.filter(pl.col("exchange_code").str.contains("MS"))
    assert tagged.height > 0
    assert tagged["symbol"].str.contains("MS[CP]").all()
    assert frame["symbol"].n_unique() == frame.height
    _valid(frame, "option_bars")


def test_czce_expiry_file_retains_zero_settlement_contract():
    # An expiry-day row can close its last position without trading. CZCE
    # publishes the row, and its negative OI change is useful evidence.
    body = (
        "郑州商品交易所期权每日行情表(2026-01-13)\n"
        "合约代码|昨结算|今开盘|最高价|最低价|今收盘|今结算|成交量(手)|持仓量|增减量|成交额(万元)|行权量\n"
        "FG602C1220|0.50|0|0|0|0|0|0|0|-21168|0|1\n"
        "小计|||||||0|0|0|0|1\n"
    ).encode()
    frame = czce.parse_options(body, date(2026, 1, 13))
    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["symbol"] == "FG2602C1220.CZC"
    assert row["settle"] == 0
    assert row["oi_change"] == -21168
    assert row["exercise_volume"] == 1
    _valid(frame, "option_bars")


def test_czce_holiday_page_is_no_file():
    with pytest.raises(FuturesDayUnavailable):
        czce.parse_futures(_read("czce_holiday.html"), date(2026, 9, 25))


# --- GFEX -------------------------------------------------------------------


def test_gfex_futures_and_subtotal_check():
    frame = gfex.parse_futures(_read("gfex_20260924_futures.json"), date(2026, 9, 24))
    assert frame.height == 12
    assert frame["symbol"].str.starts_with("SI").all()
    _valid(frame, "futures_bars")
    payload = json.loads(_read("gfex_20260924_futures.json"))
    payload["data"][-1]["volumn"] += 1
    with pytest.raises(FuturesPayloadError):
        gfex.parse_futures(json.dumps(payload).encode(), date(2026, 9, 24))


def test_gfex_options_are_fractions_with_exercise_counts():
    frame = gfex.parse_options(_read("gfex_20260924_options.json"), date(2026, 9, 24))
    assert frame["underlying_symbol"].unique().to_list() == ["SI2611.GFE"]
    assert frame["implied_vol"].drop_nulls().max() < 5
    assert frame["exercise_volume"].null_count() == 0
    _valid(frame, "option_bars")


def test_gfex_all_zero_total_means_closed():
    with pytest.raises(FuturesDayUnavailable):
        gfex.parse_futures(_read("gfex_holiday.json"), date(2026, 9, 25))


def test_dce_challenge_page_is_blocked_not_empty():
    assert looks_like_challenge(412, _read("dce_challenge_412.html"))


def test_gfex_reference_snapshot_uses_acquisition_day(monkeypatch):
    from cnequity.domain import market_time

    monkeypatch.setattr(market_time, "shanghai_today", lambda: date(2026, 9, 27))
    monkeypatch.setattr(gfex, "fetch_bytes", lambda *args, **kwargs: b"snapshot")
    seen = []

    def parse(body, day, *, kind):
        seen.append(day)
        return pl.DataFrame({"as_of": [day]})

    monkeypatch.setattr(gfex, "parse_reference", parse)
    result = gfex.fetch_gfex_reference(date(2026, 9, 4), products=["lc"], kind="option")
    assert seen == [date(2026, 9, 27)]
    assert result["as_of"][0] == date(2026, 9, 27)
