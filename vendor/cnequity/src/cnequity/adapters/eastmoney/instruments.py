"""Instrument list dates: baostock first, EastMoney push2 clist for the rest.

baostock's ``query_stock_basic`` answers every SH/SZ stock and ETF listing date
in one request; the push2 clist boards cost ~60 (A-share) and ~13 (ETF/LOF)
pages from a host that bans IPs for volume. So push2 is asked only for what
baostock left null — mostly Beijing names, which baostock does not carry, and
listings too new for it — and only for the board those names sit on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timezone

import polars as pl

from cnequity.adapters.eastmoney.clist import clist_rows_to_symbols, fetch_clist_pages
from cnequity.adapters.eastmoney.common import (
    ALL_A_FS,
    ETF_CLIST_FS,
    symbol_from_clist,
    symbol_from_clist_etf,
)
from cnequity.adapters.eastmoney.em_auth import EastMoneyClient
from cnequity.config import Config

logger = logging.getLogger(__name__)


def _parse_list_date(value: object) -> date | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        num = int(float(value))
    except (TypeError, ValueError, OverflowError):
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    if num > 1_000_000_000_000:
        try:
            return datetime.fromtimestamp(num / 1000, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(num)
    if len(text) == 8:
        try:
            return date.fromisoformat(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
        except ValueError:
            return None
    return None


def fetch_list_date_map(
    *,
    client: EastMoneyClient | None = None,
    config: Config | None = None,
    equity: bool = True,
    etf: bool = True,
) -> dict[str, date]:
    """Return symbol -> list_date for A-shares and ETFs/LOFs from EastMoney.

    *equity* / *etf* choose the boards to page, so a caller missing only one
    kind of name does not pay for the other board.
    """
    owns = client is None
    if client is None:
        client = EastMoneyClient(config=config)
    try:
        out: dict[str, date] = {}
        if equity:
            # The A-share board is primary and stays fail-loud: a broken
            # snapshot must not be mistaken for a lack of stock listing dates.
            rows = fetch_clist_pages(client, fields="f12,f13,f26", fs=ALL_A_FS)
            for sym, item in clist_rows_to_symbols(rows, symbol_resolver=symbol_from_clist):
                list_date = _parse_list_date(item.get("f26"))
                if list_date is not None:
                    out[sym] = list_date
        if not etf:
            return out

        # The ETF/LOF board is secondary enrichment; preserve stock dates when
        # that board is temporarily unavailable.
        try:
            etf_rows = fetch_clist_pages(client, fields="f12,f13,f26", fs=ETF_CLIST_FS)
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
            logger.warning("EastMoney clist ETF/LOF list_date fetch failed: %s", exc)
            return out
        for sym, item in clist_rows_to_symbols(etf_rows, symbol_resolver=symbol_from_clist_etf):
            list_date = _parse_list_date(item.get("f26"))
            if list_date is not None:
                out[sym] = list_date
        return out
    finally:
        if owns:
            client.close()


def _baostock_list_dates(
    config: Config, basics_source: Callable[[], pl.DataFrame] | None = None
) -> dict[str, date]:
    """SH/SZ stock and ETF listing dates from one baostock query; {} on failure."""
    if not config.sources.get("baostock", False):
        return {}
    try:
        if basics_source is None:
            from cnequity.adapters.baostock.instruments import fetch_instrument_basics

            basics = fetch_instrument_basics(config=config)
        else:
            basics = basics_source()
    except Exception as exc:  # noqa: BLE001 — enrichment falls through to EastMoney
        logger.warning("baostock instrument list_date lookup failed: %s", exc)
        return {}
    if basics.is_empty():
        return {}
    # A future ipoDate is an announced IPO, not a listing; leave it null so the
    # not-yet-listed placeholder logic still sees it as unlisted.
    known = basics.filter(pl.col("list_date").is_not_null() & (pl.col("list_date") <= date.today()))
    return dict(zip(known["symbol"].to_list(), known["list_date"].to_list(), strict=True))


def _fill_list_dates(df: pl.DataFrame, date_map: dict[str, date]) -> pl.DataFrame:
    if not date_map:
        return df
    enrich = pl.DataFrame(
        {"symbol": list(date_map.keys()), "_list_date_fill": list(date_map.values())},
        schema={"symbol": pl.Utf8, "_list_date_fill": pl.Date},
    )
    merged = df.join(enrich, on="symbol", how="left")
    return merged.with_columns(
        pl.coalesce(pl.col("list_date"), pl.col("_list_date_fill")).alias("list_date")
    ).drop("_list_date_fill")


def enrich_instrument_list_dates(
    config: Config,
    df: pl.DataFrame,
    *,
    basics_source: Callable[[], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """Fill null list_date on *df*: baostock first, then EastMoney for the rest.

    *basics_source* lets a caller that also needs baostock's instrument basics
    share one query instead of making the ~90s request twice.
    """
    if df.is_empty() or df.filter(pl.col("list_date").is_null()).is_empty():
        return df

    df = _fill_list_dates(df, _baostock_list_dates(config, basics_source))
    missing = df.filter(pl.col("list_date").is_null())
    if missing.is_empty() or not config.sources.get("eastmoney", True):
        return df

    if "asset_type" in missing.columns:
        kinds = set(missing["asset_type"].fill_null("stock").to_list())
        want_etf = "etf" in kinds
        want_equity = bool(kinds - {"etf"})
    else:
        want_etf = want_equity = True
    try:
        date_map = fetch_list_date_map(config=config, equity=want_equity, etf=want_etf)
    except Exception as exc:
        logger.warning("EastMoney instrument list_date enrichment failed: %s", exc)
        return df
    return _fill_list_dates(df, date_map)
