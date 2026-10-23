"""The live Beijing listing, enumerated from the exchange's own board.

TDX serves Shanghai and Shenzhen only, so nothing in the daily instruments path
can see Beijing. The gap was filled by `_merge_untdxable_instruments`, which
replays the *last* code-space sweep (`scripts/delisted_ops.py discover`) — an
occasional operator action, not a daily one. So a Beijing name listed since that
sweep never entered the catalogue at all, and a symbol that is not in the
catalogue gets no bars, no status, no anything.

Measured on 2026-09-15: the board published 343 securities and the lake held
327, missing 16 that were trading that day with real volume (森合高科, 杰理科技,
华汇智能 and 13 others) and had no row anywhere in the lake.

The board is the authority for this exchange, it is one paginated read for the
whole market, and the quote and trading-status paths already walk it — so this
costs no additional requests beyond the ones Beijing already serves us, and it
carries the 证券简称 that the TDX-shaped rows never had.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx
import polars as pl

from cnequity.adapters.bse.daily_quotes import BseMarketDataError
from cnequity.adapters.bse.trading_status import board_names
from cnequity.adapters.cninfo.listings import fetch_listing_calendar

logger = logging.getLogger(__name__)


def fetch_bse_instruments(trade_date: date, *, client=None, config=None) -> pl.DataFrame:
    """Every security the Beijing board published for *trade_date*.

    Listing dates are absent from the board. Confirmed same-day CNINFO listing
    events supply new dates; the compact preserves dates established earlier.
    """
    listed, complete = board_names(trade_date, client=client, config=config)
    if listed and not complete:
        raise BseMarketDataError("BSE instrument board is incomplete; cannot publish roster")
    symbols = sorted(listed)
    dates = {}
    if symbols and config is not None and config.sources.get("cninfo", False):
        try:
            dates = fetch_listing_calendar(config, trade_date)
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            logger.warning("BSE listing-date enrichment unavailable for %s: %s", trade_date, exc)
    return pl.DataFrame(
        {
            "symbol": symbols,
            "name": [listed[s] or None for s in symbols],
            "exchange": ["BJ"] * len(symbols),
            "asset_type": ["stock"] * len(symbols),
            "list_date": pl.Series([dates.get(s) for s in symbols], dtype=pl.Date),
            "delist_date": pl.Series([None] * len(symbols), dtype=pl.Date),
            "prev_symbol": pl.Series([None] * len(symbols), dtype=pl.Utf8),
        },
        schema_overrides={"name": pl.Utf8},
    )
