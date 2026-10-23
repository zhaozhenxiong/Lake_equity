"""TDX daily bars with pagination beyond the 800-bar API limit.

TDX reports daily-K ``vol`` in 手; the lake stores 股 (see
:mod:`cnequity.domain.units`), so the stock path multiplies by 100 here, at
the boundary. Measured over 12,182,204 curated rows, ``amount / close / vol``
had a median of 100.000 before the conversion — a lot, not a share.

The index path deliberately does **not** convert. ``client.index()`` is a
different wire call from ``client.bars()``, and its ``vol`` does not reconcile
against the sum of its constituents at any power of 100 (checked on
000001.SH: index amount is 77% of the SH stock-sum amount, but the volumes are
~300× apart, which no shares/lots reading explains). Until that unit is
pinned down, ``index_bars`` and ``sector_bars`` keep the value TDX sent and
their own contract; scaling it on a guess would only move the break.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import date, datetime

import polars as pl

from cnequity.adapters.numeric import finite_int64
from cnequity.adapters.tdx_protocol._decode import decoded_quantity_or_none
from cnequity.adapters.tdx_protocol.history_window import window_pages
from cnequity.domain.rate_limit import RateLimitSpec, source_request_slot_spec, wait_spec
from cnequity.domain.units import lots_to_shares

logger = logging.getLogger(__name__)

_PAGE_SIZE = 800
_MAX_PAGES = 1000


class TdxBarsPaginationError(RuntimeError):
    """Raised when a TDX bars page fails and the caller requires complete history."""


def _date_column(pdf: pl.DataFrame) -> str:
    return "datetime" if "datetime" in pdf.columns else "date"


def _coerce_date(val) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if hasattr(val, "date"):
        return val.date()
    if isinstance(val, str):
        return date.fromisoformat(val[:10])
    raise TypeError(f"unsupported bar date value: {val!r}")


def _page_dates(pdf: pl.DataFrame) -> list[date | None]:
    col = _date_column(pdf)
    if col not in pdf.columns:
        return [None] * len(pdf)
    dates = []
    for val in pdf[col]:
        try:
            dates.append(_coerce_date(val))
        except (TypeError, ValueError, OverflowError):
            dates.append(None)
    return dates


def _parse_bar_rows(
    pdf: pl.DataFrame,
    sym: str,
    start: date,
    end: date,
    *,
    volume_in_lots: bool = True,
) -> list[dict]:
    date_col = _date_column(pdf)
    rows: list[dict] = []
    for row in pdf.iter_rows(named=True):
        try:
            td = _coerce_date(row[date_col])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if td < start or td > end:
            continue
        try:
            open_ = float(row.get("open", 0))
            high = float(row.get("high", 0))
            low = float(row.get("low", 0))
            close = float(row.get("close", 0))
            volume = decoded_quantity_or_none(row.get("volume", row.get("vol")))
            amount = decoded_quantity_or_none(row.get("amount"))
            if volume is None or amount is None:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (open_, high, low, close, volume, amount)):
            continue
        try:
            raw_volume = finite_int64(
                volume,
                minimum=0,
                maximum=(2**63 - 1) // 100 if volume_in_lots else 2**63 - 1,
            )
        except ValueError:
            continue
        # Lot-rounded volume can hide a fund's 1..99-share trade. Positive
        # turnover disproves zero trading; leave this key for another source
        # rather than publishing a false suspension bar or guessing shares.
        if raw_volume == 0 and amount > 0:
            continue
        rows.append(
            {
                "symbol": sym,
                "trade_date": td,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": lots_to_shares(raw_volume) if volume_in_lots else raw_volume,
                "amount": amount,
            }
        )
    return rows


def fetch_bars_paginated(
    client,
    sym: str,
    start: date,
    end: date,
    *,
    rate_limit: RateLimitSpec | None = None,
    backfill: bool = False,
    on_page: Callable[[], None] | None = None,
    is_index: bool = False,
    metrics: dict[str, int] | None = None,
) -> list[dict]:
    """Fetch daily bars for *sym* in [start, end], paging through TDX history.

    Indices must use the ``index()`` call — ``bars()`` with a stock
    market id returns corrupt datetimes for index codes (e.g. 399001.SZ).

    Backfills locate old windows by date before reading consecutive pages.
    Incremental fetches retain their sequential walk from the tip.

    Stock rows come back with ``volume`` in 股; index rows keep TDX's own unit.
    See the module docstring for why the two differ.
    """
    code, exch = sym.split(".")
    market = 1 if exch == "SH" else (0 if exch == "SZ" else 2)
    all_rows: list[dict] = []

    def fetch_page(offset_pos: int) -> pl.DataFrame:
        if metrics is not None:
            metrics["requests"] = int(metrics.get("requests", 0)) + 1
            metrics["pages"] = int(metrics.get("pages", 0)) + 1
        try:
            # Pace after obtaining capacity so slow preceding calls cannot
            # release a burst of already-paced requests onto the socket.
            with source_request_slot_spec(rate_limit, metrics=metrics):
                wait_spec(rate_limit)
                if is_index:
                    raw = client.index(
                        symbol=code,
                        frequency=9,
                        start=offset_pos,
                        offset=_PAGE_SIZE,
                    )
                else:
                    raw = client.bars(
                        symbol=code,
                        frequency=9,
                        market=market,
                        start=offset_pos,
                        offset=_PAGE_SIZE,
                    )
        except Exception as exc:
            # A page is part of the requested symbol/window contract. Returning
            # earlier pages as a successful incremental fetch silently advances
            # the watermark past the missing tail, so every page failure must
            # propagate to the batch/failover layer.
            raise TdxBarsPaginationError(
                f"TDX bars page failed for {sym} at start={offset_pos}"
            ) from exc

        if raw is None or len(raw) == 0:
            return pl.DataFrame()
        if isinstance(raw, pl.DataFrame):
            return raw
        if hasattr(raw, "columns"):
            return pl.from_pandas(raw)
        return pl.DataFrame(raw)

    for pdf in window_pages(
        fetch_page,
        _page_dates,
        start,
        end,
        page_size=_PAGE_SIZE,
        page_limit=_MAX_PAGES,
        seek=backfill,
        strict_limit=True,
        detect_repeats=True,
        error=TdxBarsPaginationError,
        label=f"TDX bars for {sym}",
        limit_message=f"TDX bars pagination exceeded {_MAX_PAGES} pages for {sym}",
        on_page=on_page,
    ):
        all_rows.extend(_parse_bar_rows(pdf, sym, start, end, volume_in_lots=not is_index))

    if not all_rows:
        return []

    df = pl.DataFrame(all_rows).unique(subset=["symbol", "trade_date"], keep="last")
    if metrics is not None:
        metrics["rows_read"] = int(metrics.get("rows_read", 0)) + df.height
        metrics["bytes_read"] = int(metrics.get("bytes_read", 0)) + df.estimated_size()
    return df.sort("trade_date").to_dicts()
