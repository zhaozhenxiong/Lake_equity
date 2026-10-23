"""CFFEX daily files: parsing, self-consistency and the no-file signals."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from cnequity.adapters.futures_exchange.cffex import parse_daily_csv, parse_params_xml
from cnequity.adapters.futures_exchange.common import (
    FuturesDayUnavailable,
    FuturesPayloadError,
    looks_like_challenge,
)
from cnequity.domain.schemas import validate_dataframe, with_provenance

FIXTURES = Path(__file__).parents[1] / "fixtures" / "futures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_current_file_splits_futures_and_options():
    day = parse_daily_csv(_read("cffex_20260924.csv"), date(2026, 9, 24))
    assert sorted(day.futures["product"].unique().to_list()) == ["IF", "TS"]
    assert day.futures.height == 7
    assert day.options.height == 5
    first = day.futures.filter(pl.col("symbol") == "IF2610.CFE").row(0, named=True)
    assert first["exchange_code"] == "IF2610"
    assert first["volume"] == 25746
    # 万元 in the file, 元 in the lake.
    assert first["amount"] == pytest.approx(3441381.894 * 10_000)
    assert first["settle"] == 4433.4
    option = day.options.filter(pl.col("symbol") == "MO2610C6200.CFE").row(0, named=True)
    assert option["underlying_symbol"] == "000852.SH"
    assert option["option_type"] == "C"
    assert option["strike"] == 6200.0
    assert option["delta"] == pytest.approx(0.983)


def test_untraded_contracts_keep_settlement_but_no_prices():
    day = parse_daily_csv(_read("cffex_20260924.csv"), date(2026, 9, 24))
    idle = day.options.filter(pl.col("volume") == 0)
    assert idle.height == 2
    assert idle["close"].null_count() == 2
    assert idle["settle"].null_count() == 0


_EXPIRY_DAY = (
    "合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,今收盘,今结算,前结算,涨跌1,涨跌2,Delta\n"
    "IO2609-C-4400,108.2,110,100,2875,31.1,0,-900,108,108.38,95.2,12.8,13.18,1.0000\n"
    "IO2609-P-4600,0.4,0.6,0.2,120,0.05,0,-400,0.2,0,3.4,-3.2,-3.4,0.0000\n"
    "小计,,,,2995,31.15,0,,,,,,,--\n"
    "合计,,,,2995,31.15,0,,,,,,,--\n"
)


def test_an_option_that_expires_worthless_settles_at_zero_not_null():
    # 2026-09-18: expiring out-of-the-money options print 今结算 0 — a price.
    day = parse_daily_csv(_EXPIRY_DAY.encode(), date(2026, 9, 18))
    settles = dict(zip(day.options["symbol"], day.options["settle"], strict=True))
    assert settles == {"IO2609C4400.CFE": 108.38, "IO2609P4600.CFE": 0.0}
    stamped = with_provenance(day.options, source="futures_exchange", data_version="v1")
    assert validate_dataframe(stamped, "option_bars").height == 2


@pytest.mark.parametrize(
    ("name", "day", "futures"),
    [
        # Before options: an 「隐含波动率(%)」 column that the parser must skip.
        ("cffex_20150105.csv", date(2015, 1, 5), 7),
        # IF1005, the first session.
        ("cffex_20100416.csv", date(2010, 4, 16), 4),
    ],
)
def test_older_headers_parse_by_column_name(name, day, futures):
    parsed = parse_daily_csv(_read(name), day)
    assert parsed.futures.height == futures
    assert parsed.options.is_empty()


def test_parsed_rows_satisfy_the_stored_contract():
    day = parse_daily_csv(_read("cffex_20260924.csv"), date(2026, 9, 24))
    for dataset, frame in (("futures_bars", day.futures), ("option_bars", day.options)):
        stamped = with_provenance(
            frame.with_columns(pl.lit("futures_exchange").alias("source")),
            source="futures_exchange",
            data_version="v1",
        )
        assert validate_dataframe(stamped, dataset).height == frame.height


def test_a_file_whose_subtotal_disagrees_is_refused():
    text = _read("cffex_20260924.csv").decode("gbk")
    damaged = text.replace("小计,,,,97412,", "小计,,,,97413,", 1)
    assert damaged != text
    with pytest.raises(FuturesPayloadError, match="小计"):
        parse_daily_csv(damaged.encode("gbk"), date(2026, 9, 24))


def test_a_holiday_page_means_no_file():
    with pytest.raises(FuturesDayUnavailable):
        parse_daily_csv(_read("cffex_holiday_302.html"), date(2026, 9, 25))


def test_trading_parameters_name_listing_and_last_trading_day():
    frame = parse_params_xml(_read("cffex_jycs_20250102.xml"), date(2025, 1, 2))
    assert set(frame["kind"]) == {"future", "option"}
    option = frame.filter(pl.col("kind") == "option").row(0, named=True)
    assert option["symbol"].startswith("IO2501C")
    assert option["last_trade_date"] == date(2025, 1, 17)
    assert option["list_date"] is not None


def test_an_access_challenge_is_not_mistaken_for_an_empty_day():
    challenge = b'<html><meta id="9wq7" content="x"><script>$_ts=window["$_ts"]</script>'
    assert looks_like_challenge(200, challenge)
    assert looks_like_challenge(412, b"")
    assert not looks_like_challenge(404, b"<html>not found</html>")


class _FlakyClient:
    """Resets the connection on the first request, answers on the second."""

    calls = 0

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def request(self, method, url, **_kwargs):
        import httpx

        type(self).calls += 1
        if type(self).calls == 1:
            raise httpx.ReadError("[Errno 54] Connection reset by peer")
        return httpx.Response(200, content=b"ok", request=httpx.Request(method, url))


def test_a_dropped_connection_is_retried_once(monkeypatch):
    from cnequity.adapters.futures_exchange import common

    common.clear_cache()
    monkeypatch.setattr(common.httpx, "Client", _FlakyClient)
    monkeypatch.setattr(common.time, "sleep", lambda _s: None)
    _FlakyClient.calls = 0
    assert common.fetch_bytes("http://www.gfex.com.cn/x", method="POST", data={"d": "1"}) == b"ok"
    assert _FlakyClient.calls == 2
    common.clear_cache()


def test_a_connection_that_keeps_dropping_is_a_payload_error(monkeypatch):
    import httpx

    from cnequity.adapters.futures_exchange import common

    class _Dead(_FlakyClient):
        def request(self, method, url, **_kwargs):
            raise httpx.ReadError("reset")

    common.clear_cache()
    monkeypatch.setattr(common.httpx, "Client", _Dead)
    monkeypatch.setattr(common.time, "sleep", lambda _s: None)
    with pytest.raises(FuturesPayloadError, match="ReadError"):
        common.fetch_bytes("http://www.gfex.com.cn/y")
