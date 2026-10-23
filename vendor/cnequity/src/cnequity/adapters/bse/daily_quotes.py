"""Current daily quotes from the official Beijing Stock Exchange site.

The BSE quotation page exposes a paginated end-of-day snapshot containing
OHLCV and turnover for the listed board.  It is intentionally a *tip* source:
the endpoint returns the latest session, not a historical series.  Callers
must therefore pass the expected session and reject a response for another
date instead of stamping it onto an older partition.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Any

import httpx
import polars as pl

from cnequity.adapters.numeric import finite_int64
from cnequity.domain.http_policy import (
    record_cache_reuse,
    record_http_response,
    record_request_event,
)
from cnequity.domain.rate_limit import source_request
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.bse.cn"
_QUOTATION_PAGE = f"{_BASE_URL}/nq/quotation.html"
_QUOTATION_API = f"{_BASE_URL}/nqhqController/nqhq_en.do"
_PAGE_SIZE = 20
_MAX_PAGES = 100
_BOARD_CACHE_SECONDS = 300
_CALLBACK_RE = re.compile(r"^[A-Za-z_$][\w$]*\((.*)\);?$", re.DOTALL)
_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": _QUOTATION_PAGE,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


class BseMarketDataError(RuntimeError):
    """Raised when the official BSE quotation response is unusable."""


def _parse_jsonp(text: str) -> Any:
    payload = text.strip()
    match = _CALLBACK_RE.fullmatch(payload)
    if match:
        payload = match.group(1)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BseMarketDataError(
            f"BSE quotation response is not valid JSON/JSONP: {text[:120]!r}"
        ) from exc


def _parse_page(text: str) -> tuple[list[dict[str, Any]], int]:
    payload = _parse_jsonp(text)
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise BseMarketDataError("BSE quotation response has no page object")
    page = payload[0]
    content = page.get("content")
    if content is None:
        raise BseMarketDataError("BSE quotation response is missing content")
    if not isinstance(content, list) or not all(isinstance(row, dict) for row in content):
        raise BseMarketDataError("BSE quotation content is not a list of objects")
    if "totalElements" not in page:
        raise BseMarketDataError("BSE quotation response is missing totalElements")
    try:
        total = int(page["totalElements"])
    except (TypeError, ValueError) as exc:
        raise BseMarketDataError("BSE quotation totalElements is invalid") from exc
    if total < 0 or (content and total == 0):
        raise BseMarketDataError("BSE quotation totalElements contradicts page content")
    return content, total


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _float(value: object, *, minimum: float = 0.0) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < minimum:
        return None
    return parsed


def _row_to_quote(row: dict[str, Any], expected_date: date) -> dict[str, Any] | None:
    code = str(row.get("hqzqdm") or "").strip().zfill(6)
    if len(code) != 6 or not code.isdigit():
        return None
    trade_date = _parse_date(row.get("hqjsrq"))
    if trade_date != expected_date:
        return None
    open_ = _float(row.get("hqjrkp"), minimum=0.0)
    high = _float(row.get("hqzgcj"), minimum=0.0)
    low = _float(row.get("hqzdcj"), minimum=0.0)
    close = _float(row.get("hqzjcj"), minimum=0.0)
    amount = _float(row.get("hqcjje"), minimum=0.0)
    # 昨收: the exchange's previous close, the ex-rights reference on an ex-date.
    pre_close = _float(row.get("hqzrsp"), minimum=0.0) or None
    if None in (open_, high, low, close, amount):
        return None
    try:
        volume = finite_int64(row.get("hqcjsl"), minimum=0)
    except (TypeError, ValueError, OverflowError):
        return None
    return {
        "symbol": f"{code}.BJ",
        "trade_date": trade_date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": pre_close,
        "volume": volume,
        "amount": amount,
    }


def read_board(
    *,
    client: httpx.Client | None = None,
    config=None,
    expected_date: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Walk the whole quotation board, returning its raw rows and its own total.

    The board is a *current* snapshot with no date parameter, so this takes no
    session: callers filter on ``hqjsrq`` themselves.

    A walk that ends on an empty page before the advertised total is a
    truncated board and raises here. An *under-filled* page does not: the loop
    paginates by page offset, so it cannot see one. The returned total lets
    consumers enforce completeness before treating an absent security as
    evidence of a halt.
    """
    # The endpoint has no date parameter. Only reuse a *complete* reading
    # whose own row dates match the consumer's session. A custom client is a
    # transport boundary (notably in offline tests), so it bypasses the cache.
    if config is not None and client is None and expected_date is not None:
        path = config.meta_root / "source_cache" / "bse" / f"board-{expected_date}.json"
        with exclusive_lock(path.with_suffix(".lock")):
            if path.exists():
                try:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                    captured = datetime.fromisoformat(cached["captured_at"])
                    rows = cached["rows"]
                    total = cached["total"]
                    fresh = (
                        datetime.now(timezone.utc) - captured
                    ).total_seconds() < _BOARD_CACHE_SECONDS
                    if fresh and _complete_session(rows, total, expected_date):
                        record_cache_reuse(config, "bse", "board_snapshot")
                        return rows, total
                except (KeyError, TypeError, ValueError, OSError):
                    pass
            rows, total = _read_board_uncached(config=config)
            if _complete_session(rows, total, expected_date):
                write_json_atomic(
                    path,
                    {
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "rows": rows,
                        "total": total,
                    },
                )
            return rows, total
    return _read_board_uncached(client=client, config=config)


def _complete_session(rows: list[dict[str, Any]], total: int, day: date) -> bool:
    return (
        bool(total)
        and len(rows) >= total
        and all(_parse_date(row.get("hqjsrq")) == day for row in rows)
    )


def _request_data(page: int) -> dict[str, object]:
    """One BSE board page; also used by the bounded reachability probe."""
    return {
        "page": page,
        "type_en": '["B"]',
        "sortfield": "hqcjsl",
        "sorttype": "desc",
        "xxfcbj_en": "[2]",
        "zqdm": "",
    }


def _read_board_uncached(
    *, client: httpx.Client | None = None, config=None
) -> tuple[list[dict[str, Any]], int]:
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=20.0, follow_redirects=False, headers=_HEADERS)
    rows: list[dict[str, Any]] = []
    total = 0
    try:
        # The first request establishes the WAF cookie.  It may answer 302 to
        # the same URL; do not follow that redirect because it loops on some
        # overseas CDNs, and the cookie is enough for the JSONP endpoint.
        with source_request(config, "bse"):
            landing = client.get(_QUOTATION_PAGE)
            record_http_response(config, "bse", landing)
            if landing.status_code not in {301, 302, 307, 308}:
                landing.raise_for_status()
        first_page = True
        page = 0
        while first_page or page * _PAGE_SIZE < total:
            if page >= _MAX_PAGES:
                raise BseMarketDataError("BSE quotation pagination exceeded safety limit")
            request_data = _request_data(page)
            with source_request(config, "bse"):
                response = client.post(_QUOTATION_API, data=request_data)
                record_http_response(config, "bse", response, expected_json=True)
            if getattr(response, "status_code", 200) in {301, 302, 307, 308}:
                # The site occasionally refreshes its WAF cookie with a
                # same-URL redirect. Re-establish the page cookie once; never
                # follow an arbitrary Location header into another endpoint.
                with source_request(config, "bse"):
                    landing = client.get(_QUOTATION_PAGE)
                    record_http_response(config, "bse", landing)
                    if landing.status_code not in {301, 302, 307, 308}:
                        landing.raise_for_status()
                with source_request(config, "bse"):
                    record_request_event(config, "bse", "retry")
                    response = client.post(_QUOTATION_API, data=request_data)
                    record_http_response(config, "bse", response, expected_json=True)
            response.raise_for_status()
            page_rows, page_total = _parse_page(response.text)
            if total and page_total and page_total != total:
                raise BseMarketDataError(
                    f"BSE quotation totalElements changed during pagination ({total} -> {page_total})"
                )
            # An empty tail page reports zero; keep the last real figure so it
            # cannot erase the number the walk is being measured against.
            total = page_total or total
            rows.extend(page_rows)
            if not page_rows:
                if page * _PAGE_SIZE < total:
                    raise BseMarketDataError(
                        "BSE quotation pagination ended before the advertised total "
                        f"({page * _PAGE_SIZE}/{total} rows)"
                    )
                break
            first_page = False
            page += 1
    finally:
        if owns_client:
            client.close()
    return rows, total


def fetch_daily_quotes(
    trade_date: date,
    *,
    symbols: list[str] | set[str] | None = None,
    client: httpx.Client | None = None,
    config=None,
) -> pl.DataFrame:
    """Fetch the BSE end-of-day quote snapshot for *trade_date*.

    The endpoint is a current snapshot.  If its session date differs from
    *trade_date*, an empty frame is returned; this makes accidental historical
    backfill stamping impossible.  ``symbols`` limits the returned rows after
    the board-wide pagination, while the response remains board-authoritative.
    """
    wanted = set(symbols) if symbols is not None else None
    raw_rows, total = read_board(client=client, config=config, expected_date=trade_date)
    if total and len(raw_rows) != total:
        raise BseMarketDataError(
            f"BSE quotation pagination returned {len(raw_rows)}/{total} advertised rows"
        )
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        quote = _row_to_quote(raw, trade_date)
        if quote is not None and (wanted is None or quote["symbol"] in wanted):
            rows.append(quote)

    if not rows:
        return pl.DataFrame(
            schema={
                "symbol": pl.Utf8,
                "trade_date": pl.Date,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "pre_close": pl.Float64,
                "volume": pl.Int64,
                "amount": pl.Float64,
            }
        )
    return (
        pl.DataFrame(rows, schema_overrides={"pre_close": pl.Float64})
        .unique(subset=["symbol", "trade_date"], keep="last")
        .sort(["trade_date", "symbol"])
    )


__all__ = ["BseMarketDataError", "fetch_daily_quotes"]
