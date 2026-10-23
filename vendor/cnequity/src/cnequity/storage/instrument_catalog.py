"""Read the committed security master without ingestion or network fallbacks."""

from __future__ import annotations

import logging
from datetime import date

import polars as pl

from cnequity.config import Config
from cnequity.storage.read_context import ReadContext, read_root


def load_curated_instruments(
    config: Config, read_context: ReadContext | None = None
) -> pl.DataFrame | None:
    """Read the merge-style instrument catalog across all surviving shards."""
    root = read_root(config, "instruments", read_context)
    if not root.exists() or not any(root.rglob("*.parquet")):
        return None
    from cnequity.query.canonical import dedupe_by_primary_key
    from cnequity.query.parquet_scan import collect_parquet_root

    try:
        frame = collect_parquet_root(root, hive=False, committed=False)
    except FileNotFoundError:
        return None
    return dedupe_by_primary_key(frame, "instruments")


logger = logging.getLogger(__name__)


def listed_fund_symbols(
    instruments: pl.DataFrame, on: date, *, start: date | None = None
) -> list[str]:
    """Fund quote scope from the security master, independent of ETF eligibility.

    Unknown listing dates are still requested and reported as unknown by the
    coverage checker. A classification directory failure must not erase their
    quote history. Known prelisting and delisted names stay out of a live sweep.
    """
    if not {"symbol", "asset_type"} <= set(instruments.columns):
        raise ValueError("fund ingest requires instruments with symbol and asset_type")
    frame = instruments
    for column in ("list_date", "delist_date"):
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Date).alias(column))
    from cnequity.domain.symbols import is_subscription_placeholder

    frame = frame.filter(
        pl.col("asset_type").is_in(["etf", "lof"])
        & pl.col("symbol").str.contains(r"\.(SH|SZ)$")
        & (pl.col("list_date").is_null() | (pl.col("list_date") <= on))
        & (pl.col("delist_date").is_null() | (pl.col("delist_date") > (start or on)))
    )
    return sorted(
        row["symbol"]
        for row in frame.unique("symbol").iter_rows(named=True)
        if not is_subscription_placeholder(row.get("name"), row["symbol"])
    )


def load_curated_trading_status(
    config: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    include_inferred: bool = False,
    read_context: ReadContext | None = None,
) -> pl.DataFrame | None:
    """Load the available status evidence without making a network request.

    ``derived_bar_gap`` rows are left out unless ``include_inferred``: they
    are inferred from the very bars this evidence is used to judge, so a
    session that lost its Beijing bars in a failed run came back as 223
    "suspensions" that excused the gap, kept it out of the next fetch and out
    of the coverage gate (2026-08-07).

    ``trading_status`` is an advisory but independently fetched daily
    snapshot.  Daily-bar routing may use it to prove that a missing symbol was
    suspended; it must never synthesize a status by treating an absent row as
    ``normal``.  A corrupt or absent status root therefore returns ``None``
    and leaves the symbol in the strict unknown bucket.
    """
    root = read_root(config, "trading_status", read_context)
    if not root.exists() or not any(root.rglob("*.parquet")):
        return None
    try:
        from cnequity.query.canonical import dedupe_by_primary_key
        from cnequity.query.parquet_scan import collect_parquet_root

        frame = collect_parquet_root(
            root,
            partition_col="trade_date",
            start=start,
            end=end,
            symbols=symbols,
            committed=False,
        )
    except (FileNotFoundError, OSError, pl.exceptions.PolarsError, ValueError) as exc:
        logger.warning("curated trading_status scan failed; status evidence unavailable: %s", exc)
        return None
    required = {"symbol", "trade_date", "is_trading"}
    if not required.issubset(frame.columns):
        logger.warning(
            "curated trading_status lacks required evidence columns: %s",
            sorted(required - set(frame.columns)),
        )
        return None
    if not include_inferred and "source" in frame.columns:
        from cnequity.domain.trading_status import DERIVED_BAR_GAP_SOURCE

        # Before the canonical pick, so an independent row on the same key
        # surfaces instead of the inference that outranked it.
        frame = frame.filter(
            pl.col("source").is_null() | (pl.col("source") != DERIVED_BAR_GAP_SOURCE)
        )
    return dedupe_by_primary_key(frame, "trading_status")
