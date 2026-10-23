"""Factual ETF/LOF listing dates, independent of research eligibility."""

from __future__ import annotations

import json
from datetime import date

import httpx

from cnequity.adapters.exchange.etf_profiles import (
    SSE_REFERER,
    SSE_URL,
    SZSE_FUNDS_REFERER,
    SZSE_URL,
    _code,
    _get_official_response,
    _text,
    _workbook_records,
)
from cnequity.config import Config


def parse_sse_fund_listings(payload: bytes) -> dict[str, date]:
    response = json.loads(payload)
    rows = response.get("result")
    if (
        response.get("actionErrors")
        or response.get("fieldErrors")
        or not isinstance(rows, list)
        or not rows
        or int((response.get("pageHelp") or {}).get("total") or 0) != len(rows)
    ):
        raise ValueError("SSE fund directory is empty or incomplete")
    out = {}
    seen = set()
    for item in rows:
        # 00 = ETF; 10 = LOF. Other listed fund classes have separate contracts.
        if str(item.get("fundType")) not in {"00", "10"}:
            continue
        symbol = f"{_code(item.get('fundCode'))}.SH"
        if symbol in seen:
            raise ValueError(f"SSE fund directory duplicates {symbol}")
        seen.add(symbol)
        listed = _text(item.get("listingDate"))
        if listed:
            out[symbol] = date.fromisoformat(f"{listed[:4]}-{listed[4:6]}-{listed[6:8]}")
    if not out:
        raise ValueError("SSE fund directory has no ETF/LOF listing dates")
    return out


def parse_szse_fund_listings(payload: bytes) -> dict[str, date]:
    rows = _workbook_records(payload)
    required = {"基金代码", "基金类别", "上市日期"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("SZSE fund directory is empty or missing listing columns")
    out = {}
    seen = set()
    for item in rows:
        if _text(item.get("基金类别")) not in {"ETF", "LOF"}:
            continue
        symbol = f"{_code(item.get('基金代码'))}.SZ"
        if symbol in seen:
            raise ValueError(f"SZSE fund directory duplicates {symbol}")
        seen.add(symbol)
        listed = _text(item.get("上市日期"))
        if listed:
            out[symbol] = date.fromisoformat(listed)
    if not out:
        raise ValueError("SZSE fund directory has no ETF/LOF listing dates")
    return out


def fetch_exchange_fund_listings(
    config: Config, *, exchanges: set[str], client: httpx.Client | None = None
) -> tuple[dict[str, date], list[tuple[str, bytes, dict, str]]]:
    """Fetch complete requested boards; return exact response bytes for archival."""
    queries = [
        (
            "SH",
            SSE_URL,
            SSE_REFERER,
            {
                "isPagination": "true",
                "pageHelp.pageSize": "2000",
                "pageHelp.pageNo": "1",
                "sqlId": "FUND_LIST",
                "fundType": "",
                "subClass": "",
            },
            parse_sse_fund_listings,
            "json",
        ),
        (
            "SZ",
            SZSE_URL,
            SZSE_FUNDS_REFERER,
            {"SHOWTYPE": "xlsx", "CATALOGID": "1000_lf", "TABKEY": "tab1"},
            parse_szse_fund_listings,
            "xlsx",
        ),
    ]
    owns = client is None
    if client is None:
        client = httpx.Client(timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    try:
        dates = {}
        wire = []
        for exchange, url, referer, params, parse, kind in queries:
            if exchange not in exchanges:
                continue
            response = _get_official_response(config, client, url, params=params, referer=referer)
            dates.update(parse(response.content))
            wire.append((url, response.content, params, kind))
        return dates, wire
    finally:
        if owns:
            client.close()
