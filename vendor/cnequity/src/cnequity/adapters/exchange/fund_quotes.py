"""Historical exchange fund coverage evidence; no inferred OHLC or quantity."""

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from cnequity.adapters.exchange.etf_profiles import _workbook_records
from cnequity.domain.symbols import is_etf_symbol

URL = "https://www.szse.cn/api/report/ShowReport"
REFERER = "https://fund.szse.cn/marketdata/trade/index.html"
CATALOG = "1815_stock_lf_snapshot"
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_REFERER = "https://etf.sse.com.cn/fundlist/funddetail/index.shtml"
SSE_SQL_ID = "COMMON_SSE_CP_GPJCTPZ_GPLB_CJGK_MRGK_C"


def parameters(day: date) -> dict:
    return {
        "SHOWTYPE": "xlsx",
        "CATALOGID": CATALOG,
        "TABKEY": "tab1",
        "txtBeginDate": str(day),
        "txtEndDate": str(day),
    }


def sse_parameters(symbol: str, day: date) -> dict:
    return {"sqlId": SSE_SQL_ID, "SEC_CODE": symbol.removesuffix(".SH"), "TX_DATE": str(day)}


@lru_cache(maxsize=256)
def sse_no_print_symbols(payload: bytes, day: date, symbol: str) -> frozenset[str]:
    """Zero auction prices establish absence of an auction bar.

    ACCU_TRADE_DAYS is not evidence: even traded daily responses contain zero.
    Rounded totals alone cannot prove absence. Whole-market turnover may be
    positive without auction OHLC, as in the existing exchange quote adapter;
    such totals cannot supply prices or quantities for an auction bar.
    """
    response = json.loads(payload)
    if not isinstance(response, dict):
        raise ValueError("SSE fund report is not an object")
    rows = response.get("result")
    if (
        response.get("actionErrors")
        or response.get("fieldErrors")
        or response.get("sqlId") != SSE_SQL_ID
        or not isinstance(rows, list)
        or len(rows) != 1
    ):
        raise ValueError("SSE fund report is missing or ambiguous")
    row = rows[0]
    if not isinstance(row, dict):
        raise ValueError("SSE fund report row is not an object")
    if (
        row.get("SEC_CODE") != symbol.removesuffix(".SH")
        or row.get("TX_DATE") != day.strftime("%Y%m%d")
        or row.get("DATE_TYPE") != "D"
        or not symbol.endswith(".SH")
        or not is_etf_symbol(symbol.removesuffix(".SH"), "SH")
    ):
        raise ValueError("SSE fund report identity differs")
    try:
        values = {
            field: Decimal(str(row.get(field)))
            for field in (
                "OPEN_PRICE",
                "HIGH_PRICE",
                "LOW_PRICE",
                "CLOSE_PRICE",
                "TRADE_AMT",
                "TRADE_VOL",
                "CHANGE_PRICE",
            )
        }
    except InvalidOperation:
        return frozenset()
    if not all(value.is_finite() for value in values.values()):
        return frozenset()
    if (
        values["CLOSE_PRICE"] > 0
        and values["TRADE_AMT"] >= 0
        and values["TRADE_VOL"] >= 0
        and all(
            values[field] == 0
            for field in ("OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CHANGE_PRICE")
        )
    ):
        return frozenset({symbol})
    return frozenset()


def report_no_print_symbols(payload: bytes, record: dict, day: date) -> frozenset[str]:
    """Bind each supported parser to its exact archive request identity."""
    if (
        not isinstance(record, dict)
        or record.get("source") != "exchange"
        or record.get("dataset") != "daily_bars"
    ):
        raise ValueError("invalid fund report provenance")
    params = record.get("request_params")
    if record.get("url") == URL and params == parameters(day):
        return no_print_symbols(payload, day)
    if record.get("url") == SSE_URL and isinstance(params, dict):
        symbol = str(params.get("SEC_CODE", "")) + ".SH"
        if params == sse_parameters(symbol, day):
            return sse_no_print_symbols(payload, day, symbol)
    raise ValueError("unsupported fund report identity")


@lru_cache(maxsize=64)
def no_print_symbols(payload: bytes, day: date) -> frozenset[str]:
    """Accept explicit zero-yuan turnover with a positive unchanged reference.

    Omitted codes, missing amounts and rounded volume are not absence proofs.
    This report cannot establish a halt or risk-warning label.
    """
    try:
        rows = _workbook_records(payload)
    except (BadZipFile, ParseError, EOFError) as exc:
        raise ValueError("SZSE fund report is not a usable workbook") from exc
    required = {"交易日期", "证券代码", "前收", "今收", "成交金额(元)"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("SZSE fund report missing required fields")
    seen, out = set(), set()
    for row in rows:
        if row.get("交易日期") != str(day):
            raise ValueError("SZSE fund report contains a different session")
        code = str(row.get("证券代码") or "")
        if len(code) != 6 or not code.isdigit() or code in seen:
            raise ValueError("SZSE fund report contains invalid or duplicate codes")
        seen.add(code)
        if not is_etf_symbol(code, "SZ"):
            continue
        try:
            previous, close, amount = (
                Decimal(str(row.get(field)).replace(",", ""))
                for field in ("前收", "今收", "成交金额(元)")
            )
        except InvalidOperation:
            continue
        if all(value.is_finite() for value in (previous, close, amount)):
            if amount == 0 and close == previous and close > 0:
                out.add(f"{code}.SZ")
    return frozenset(out)
