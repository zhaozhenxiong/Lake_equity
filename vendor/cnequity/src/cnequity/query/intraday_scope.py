"""Configured intraday universe; read-only unless ingestion supplies symbols."""

from __future__ import annotations

import polars as pl

from cnequity.config import Config
from cnequity.storage.instrument_catalog import load_curated_instruments


class MinuteBarsScopeError(RuntimeError):
    """Raised when the configured scope cannot be resolved to symbols."""


def _index_members(config: Config, index_symbol: str) -> list[str]:
    """Latest known constituents of *index_symbol* from ``index_constituents``."""
    from cnequity.query.parquet_scan import dataset_has_parquet, scan_parquet_root

    root = config.curated_root / "index_constituents"
    if not dataset_has_parquet(root):
        raise MinuteBarsScopeError(
            f"minute_bars scope 'index:{index_symbol}' needs the index_constituents "
            "dataset, which is empty — run `cne run daily` (or `cne backfill "
            "index_constituents`) first, or set [minute_bars].scope = 'watchlist'"
        )
    df = (
        scan_parquet_root(root, partition_col="as_of_date", hive=False)
        .filter(pl.col("index_symbol") == index_symbol)
        .select("symbol", "as_of_date")
        .collect()
    )
    if df.is_empty():
        raise MinuteBarsScopeError(
            f"index_constituents holds no rows for {index_symbol!r}; "
            "check the index symbol or pick another scope"
        )
    latest = df["as_of_date"].max()
    return sorted(df.filter(pl.col("as_of_date") == latest)["symbol"].unique().to_list())


def resolve_scope(config: Config, *, all_symbols=None) -> list[str]:
    """Symbols the intraday capture covers, per ``[minute_bars].scope``.

    ``index:<symbol>`` — that index's latest constituents (the default;
    沪深300 is ~300 names, about 2MB a day at 1m).
    ``watchlist`` — exactly ``[minute_bars].symbols``.
    ``all`` — the whole universe. ~1.3M rows and ~30MB a day; opt in knowingly.
    """
    scope = (config.minute_bars_scope or "").strip()
    if scope == "all":
        # BJ has no TDX intraday route at all, so it would be all failures.
        if all_symbols is None:
            frame = load_curated_instruments(config)
            if frame is None:
                raise MinuteBarsScopeError("minute scope requires committed instruments")
            symbols = frame["symbol"].to_list()
        else:
            symbols = all_symbols()
        from cnequity.domain.market_profile import served

        return served("tdx_intraday", symbols)
    if scope == "watchlist":
        symbols = [s.strip() for s in config.minute_bars_symbols if s.strip()]
        if not symbols:
            raise MinuteBarsScopeError(
                "[minute_bars].scope = 'watchlist' but [minute_bars].symbols is empty"
            )
        return symbols
    if scope.startswith("index:"):
        return _index_members(config, scope.split(":", 1)[1].strip())
    raise MinuteBarsScopeError(
        f"unknown [minute_bars].scope {scope!r} (expected 'all', 'watchlist', or 'index:<symbol>')"
    )
