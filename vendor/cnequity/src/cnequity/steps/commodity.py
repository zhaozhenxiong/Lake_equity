"""Commodity futures continuous bars (L1-adjacent)."""

from __future__ import annotations

from datetime import date

import polars as pl

from cnequity.adapters.eastmoney.commodity_bars import (
    DEFAULT_BACKFILL_START,
    fetch_commodity_bars,
    fetch_commodity_bars_range,
)
from cnequity.config import Config
from cnequity.orchestrator.outcomes import SourceUnavailableError
from cnequity.orchestrator.registry import register_step
from cnequity.steps.http_common import run_incremental_fetched, write_fetched


def _backfill_range(config: Config, trade_date: date, run_id: str) -> dict:
    """Write a whole backfill window from one range fetch.

    Both vendors answer a contract with its entire history in one request, so
    the adapter returns every session of the window at once. The generic
    backfill path accepts only rows dated the run day and rejected all of
    them, which is why no domestic history before the daily run ever landed.
    Rows are held to the requested window instead.
    """
    start = getattr(config, "_backfill_start", None) or DEFAULT_BACKFILL_START
    end = min(getattr(config, "_backfill_end", None) or trade_date, trade_date)
    if start > end:
        return {"rows_read": 0, "rows_written": 0}
    df = fetch_commodity_bars_range(start, end, config=config, strict=True)
    if df.is_empty():
        raise SourceUnavailableError(
            f"commodity_bars: no rows returned for {start.isoformat()}..{end.isoformat()}"
        )
    dates = df.get_column("trade_date").cast(pl.Date, strict=False)
    outside = int((dates.is_null() | (dates < start) | (dates > end)).sum())
    if outside:
        raise RuntimeError(
            f"commodity_bars: backfill {start.isoformat()}..{end.isoformat()} returned "
            f"{outside} row(s) with a trade_date outside the window"
        )
    return write_fetched(config, run_id, "commodity_bars", df, source="eastmoney")


@register_step("commodity_bars", group="macro_risk")
def step_commodity_bars(config: Config, trade_date: date, run_id: str, context: dict) -> dict:
    em = bool(config.sources.get("eastmoney", True))
    sina = bool(config.sources.get("sina", True))
    if not em and not sina:
        raise SourceUnavailableError("commodity_bars: both eastmoney and sina sources disabled")
    if getattr(config, "_backfill", False):
        return _backfill_range(config, trade_date, run_id)
    return run_incremental_fetched(
        config,
        trade_date,
        run_id,
        "commodity_bars",
        lambda d: fetch_commodity_bars(d, config=config, strict=True),
        # Row-level ``source`` is set by adapters (eastmoney / sina); this is
        # only the fallback stamp when a frame lacks the column.
        source="eastmoney",
        allow_empty=False,
    )
