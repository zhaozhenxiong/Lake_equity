"""DCE via Sina: history and batch quote parsing, and route selection."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import polars as pl

from cnequity.adapters.sina import dce_futures
from cnequity.config import Config
from cnequity.domain.schemas import validate_dataframe, with_provenance
from cnequity.steps import derivatives

FIXTURES = Path(__file__).parents[1] / "fixtures" / "futures"


def _history_payload() -> list[dict]:
    text = (FIXTURES / "sina_dce_M2001.txt").read_bytes().decode("gbk")
    return json.loads(re.search(r"\[.*\]", text, re.S).group(0))


def test_history_is_halved_before_2020_and_leaves_what_it_cannot_know_null():
    rows = dce_futures._history_rows("M2001", _history_payload())
    before = rows[date(2019, 12, 31)]
    after = rows[date(2020, 1, 2)]
    assert before["open_interest"] == 19722 // 2
    assert before["volume"] == 46148 // 2
    # 6463 is odd: 2020 counts are one-sided and pass through unchanged.
    assert after["open_interest"] == 6463
    assert before["amount"] is None and before["pre_settle"] is None
    assert before["symbol"] == "M2001.DCE"
    assert before["source"] == "sina"


def test_batch_quote_rows_are_dated_and_untraded_ones_have_no_prices():
    text = (FIXTURES / "sina_dce_quotes_20260924.txt").read_bytes().decode("gbk")
    frame = dce_futures.parse_quotes(text, date(2026, 9, 24))
    assert set(frame["symbol"]) == {"M2701.DCE", "I2701.DCE", "M2711.DCE"}
    m = frame.filter(pl.col("symbol") == "M2701.DCE").row(0, named=True)
    # Checked against the per-contract history for the same session.
    assert (m["settle"], m["pre_settle"], m["open_interest"], m["volume"]) == (
        3405.0,
        3414.0,
        2662957,
        1112326,
    )
    idle = frame.filter(pl.col("symbol") == "M2711.DCE").row(0, named=True)
    assert idle["close"] is None and idle["settle"] == 3350.0
    stamped = with_provenance(frame, source="sina", data_version="v1")
    assert validate_dataframe(stamped, "futures_bars").height == 3
    # A quote dated another session contributes nothing.
    assert dce_futures.parse_quotes(text, date(2026, 9, 23)).is_empty()


def test_candidates_cover_thirteen_months_of_every_product():
    codes = dce_futures.candidate_codes(date(2026, 12, 15))
    assert "M2612" in codes and "M2712" in codes and "M2801" not in codes
    assert len(codes) == 13 * len(dce_futures.PRODUCTS)


def test_dce_route_decides_the_reader_and_whether_dce_is_asked(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    cfg.futures_enabled = True
    assert "DCE" in derivatives.enabled_exchanges(cfg)
    assert derivatives.reader(cfg, "DCE").first_session("options") is None
    cfg.futures_dce_route = "official"
    assert derivatives.reader(cfg, "DCE") is derivatives.DCE_OFFICIAL
    cfg.futures_dce_route = "off"
    assert "DCE" not in derivatives.enabled_exchanges(cfg)
    # INE is published by SHFE, so asking for it enables the SHFE reader.
    cfg.futures_exchanges = ["INE"]
    assert derivatives.enabled_exchanges(cfg) == ["SHF"]


def test_a_quote_is_only_an_answer_for_its_latest_session():
    text = (FIXTURES / "sina_dce_quotes_20260924.txt").read_bytes().decode("gbk")
    stale = text.replace("连,豆粕,2026-09-24,1,,,,,,,,,0.000", "连,豆粕,2026-09-23,1,,,,,,,,,0.000")
    assert dce_futures.quote_session(stale) == date(2026, 9, 24)
    # The one contract still dated 09-23 does not make the quote 09-23's.
    assert dce_futures.parse_quotes(stale, date(2026, 9, 23)).is_empty()


def test_experimental_official_mapping_requires_numeric_fields_and_published_totals():
    import pytest

    from cnequity.adapters.futures_exchange.common import FuturesPayloadError
    from cnequity.adapters.futures_exchange.dce import parse_futures

    # Synthetic contract fixture: validates fail-closed behavior, not the live
    # endpoint's undocumented field mapping or coverage.
    row = {
        "contractId": "m2701",
        "variety": "豆粕",
        "volumn": 10,
        "openInterest": 20,
        "turnover": 300000,
        "clearPrice": 3000,
        "lastClear": 3000,
        "open": 3000,
        "high": 3000,
        "low": 3000,
        "close": 3000,
        "diffI": 0,
    }
    total = {"variety": "总计", "volumn": 10, "openInterest": 20, "turnover": 300000}

    def payload(rows):
        return json.dumps({"data": rows}).encode()

    assert parse_futures(payload([row, total]), date(2026, 9, 24)).height == 1
    with pytest.raises(FuturesPayloadError, match="grand total"):
        parse_futures(payload([row]), date(2026, 9, 24))
    with pytest.raises(FuturesPayloadError, match="total mismatch"):
        parse_futures(payload([row, {**total, "volumn": 11}]), date(2026, 9, 24))
    with pytest.raises(FuturesPayloadError, match="numeric fields"):
        parse_futures(payload([{**row, "openInterest": None}, total]), date(2026, 9, 24))
