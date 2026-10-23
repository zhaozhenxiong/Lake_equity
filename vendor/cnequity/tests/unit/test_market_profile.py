from datetime import date

import polars as pl
import pytest

from cnequity.domain.market_profile import (
    BJ,
    BSE_FIRST_SESSION,
    COVERAGE,
    block_trade_bars_expr,
    profile_for,
    served,
    serves,
    serves_expr,
    unserved,
    unserved_exchanges,
)


def test_beijing_eras_are_contiguous_and_end_in_the_exchange():
    eras = BJ.eras
    assert [e.key for e in eras] == ["neeq", "neeq_select", "bse"]
    for before, after in zip(eras, eras[1:], strict=False):
        assert (after.start - before.end).days >= 1
    assert BSE_FIRST_SESSION == date(2021, 11, 15)
    assert BJ.era_on(date(2020, 7, 24)).key == "neeq"
    assert BJ.era_on(date(2021, 1, 4)).key == "neeq_select"
    assert BJ.era_on(date(2026, 9, 30)).price_limit == 0.30
    assert profile_for("600519.SH").era_on(date(2026, 9, 30)).key == "main"


def test_coverage_splits_symbols_by_capability():
    symbols = ["600519.SH", "000001.SZ", "920000.BJ"]
    assert served("baostock", symbols) == ["600519.SH", "000001.SZ"]
    assert unserved("baostock", symbols) == ["920000.BJ"]
    assert served("bse_boards", symbols) == ["920000.BJ"]
    assert not serves("tdx_intraday", "920000.BJ")
    assert unserved_exchanges("baostock") == ("BJ",)
    frame = pl.DataFrame({"symbol": symbols})
    assert frame.filter(serves_expr("cninfo_issuer_directory"))["symbol"].to_list() == symbols[:2]
    assert frame.filter(block_trade_bars_expr())["symbol"].to_list() == ["920000.BJ"]


def test_an_unknown_capability_is_an_error_not_a_guess():
    assert "baostok" not in COVERAGE
    with pytest.raises(KeyError):
        serves("baostok", "600519.SH")
