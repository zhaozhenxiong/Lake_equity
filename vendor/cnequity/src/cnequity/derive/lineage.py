"""What a backfill changed, and whether the data derived from it has caught up.

Derived datasets used to catch up with a backfill by accident: the next daily
run refreshed adjustment factors only for symbols with an ex-date on the latest
session, a cache older than their newest ex-date, or a capped batch of
uncovered history. A corporate action backfilled into the past matched none of
those, so its factors could stay wrong indefinitely. The lineage is explicit
instead:

    corporate_actions (committed)  →  changed symbols  →  adj_factors(changed)

`cne backfill corporate_actions` fingerprints the published actions before and
after, re-derives factors for exactly the symbols whose factor-relevant terms
changed, and then checks that each of them has factors through its latest
traded bar. `cne backfill daily_bars` does the same over the bars it touched:

    daily_bars (committed)  →  changed symbols + months  →  adj_factors(realign)
                                                         →  suspensions(changed window)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import polars as pl

from cnequity.config import Config

#: The action terms the factor derivation reads. Payment dates and provenance
#: change nothing about a factor, so a payment-date repair triggers no derive.
FACTOR_TERMS = (
    "cash_dividend",
    "bonus_ratio",
    "transfer_ratio",
    "split_factor",
    "allotment_ratio",
    "allotment_price",
    "reference_price",
)
_KEY = ("symbol", "ex_date", "action_type")


def _load(config: Config, dataset: str, **kwargs) -> pl.DataFrame:
    from cnequity.query.reader import ReaderError, load

    try:
        return load(dataset, config=config, **kwargs)
    except ReaderError:
        return pl.DataFrame()


def corporate_action_fingerprint(config: Config) -> pl.DataFrame:
    """One hash per published action key over the terms a factor depends on."""
    actions = _load(config, "corporate_actions")
    if actions.is_empty():
        return pl.DataFrame(schema={"symbol": pl.Utf8, "_key": pl.Utf8, "_fp": pl.UInt64})
    terms = [c for c in FACTOR_TERMS if c in actions.columns]
    return actions.select(
        pl.col("symbol"),
        pl.concat_str([pl.col(c).cast(pl.Utf8) for c in _KEY], separator="|").alias("_key"),
        pl.struct(terms).hash().alias("_fp"),
    )


def changed_symbols(before: pl.DataFrame, after: pl.DataFrame) -> list[str]:
    """Symbols with an action added, removed or re-termed between two fingerprints."""
    joined = before.join(after, on="_key", how="full", suffix="_after", coalesce=True)
    changed = joined.filter(
        pl.col("_fp").is_null()
        | pl.col("_fp_after").is_null()
        | (pl.col("_fp") != pl.col("_fp_after"))
    )
    symbols = changed.select(pl.coalesce("symbol", "symbol_after")).to_series()
    return sorted(set(symbols.drop_nulls().to_list()))


#: The bar fields downstream derives read: factor alignment follows traded
#: dates, computed factors read close and pre_close, and the suspension derive
#: reads which sessions traded.
BAR_TERMS = ("close", "pre_close", "volume")


def daily_bar_fingerprint(config: Config, start: date, end: date) -> pl.DataFrame:
    """One hash per (symbol, month) over the committed bars in ``[start, end]``.

    Monthly, not per row: a decade of full-market bars stays a few hundred
    thousand rows, yet a change still localises to the month that holds it,
    which bounds the suspension re-derive.
    """
    from cnequity.query.parquet_scan import dataset_has_parquet, scan_parquet_root

    empty = pl.DataFrame(
        schema={"symbol": pl.Utf8, "month": pl.Date, "_n": pl.UInt32, "_fp": pl.UInt64}
    )
    root = config.curated_root / "daily_bars"
    if not dataset_has_parquet(root):
        return empty
    # Committed bars are already canonical, so no primary-key dedupe: on a
    # decade of full-market bars that sort tripled peak memory, while the
    # streaming group-by below stays near one gigabyte. A stray duplicate
    # could only flag a symbol for an unneeded realign, never hide a change.
    bars = scan_parquet_root(root, partition_col="trade_date", start=start, end=end)
    names = bars.collect_schema().names()
    terms = ["trade_date", *(c for c in BAR_TERMS if c in names)]
    return (
        bars.select(
            "symbol",
            pl.col("trade_date").dt.truncate("1mo").alias("month"),
            pl.struct(terms).hash().alias("_h"),
        )
        .group_by("symbol", "month")
        .agg(pl.len().alias("_n"), pl.col("_h").sum().alias("_fp"))
        .collect(engine="streaming")
    )


def changed_bar_scope(
    before: pl.DataFrame, after: pl.DataFrame
) -> tuple[list[str], date | None, date | None]:
    """Symbols whose bars changed, and the first and last month that changed."""
    joined = before.join(after, on=["symbol", "month"], how="full", suffix="_after", coalesce=True)
    changed = joined.filter(
        pl.col("_fp").is_null()
        | pl.col("_fp_after").is_null()
        | (pl.col("_fp") != pl.col("_fp_after"))
        | (pl.col("_n") != pl.col("_n_after"))
    )
    if changed.is_empty():
        return [], None, None
    months = changed.get_column("month")
    first, last = months.min(), months.max()
    return sorted(set(changed.get_column("symbol").to_list())), first, last


@dataclass
class FactorSync:
    """Outcome of re-deriving factors for the symbols a backfill changed."""

    affected: list[str]
    updated: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    lagging: list[str] = field(default_factory=list)
    rows: int = 0

    @property
    def synchronized(self) -> bool:
        return not (self.failed or self.lagging)

    def as_dict(self) -> dict:
        return {
            "affected_symbols": len(self.affected),
            "processed": len(self.affected),
            "updated": len(self.updated),
            "skipped": self.skipped,
            "failed": self.failed,
            "lagging": self.lagging,
            "rows_written": self.rows,
            "synchronized": self.synchronized,
        }


def verify_factor_sync(
    config: Config, affected: list[str], *, failed: list[str], rows: int
) -> FactorSync:
    """Check each affected symbol has factors through its latest traded bar.

    A symbol with no traded bar has nothing to adjust, and a CDR has no factor
    source; both are skipped rather than counted as failures.
    """
    from cnequity.derive.adj_factors import _is_cdr

    sync = FactorSync(affected=list(affected), rows=rows)
    failed_set = {item.rsplit(":", 1)[0] for item in failed} & set(affected)
    bars = _load(config, "daily_bars", symbols=list(affected))
    if not bars.is_empty() and "volume" in bars.columns:
        bars = bars.filter(pl.col("volume") > 0)
    last_bar = (
        dict(bars.group_by("symbol").agg(pl.col("trade_date").max()).iter_rows())
        if not bars.is_empty()
        else {}
    )
    factors = _load(config, "adj_factors", symbols=list(affected))
    last_factor = (
        dict(factors.group_by("symbol").agg(pl.col("trade_date").max()).iter_rows())
        if not factors.is_empty()
        else {}
    )
    for symbol in affected:
        if _is_cdr(symbol):
            sync.skipped[symbol] = "cdr"
        elif symbol not in last_bar:
            sync.skipped[symbol] = "no_bars"
        elif symbol in failed_set:
            sync.failed.append(symbol)
        elif last_factor.get(symbol) is None or last_factor[symbol] < last_bar[symbol]:
            sync.lagging.append(symbol)
        else:
            sync.updated.append(symbol)
    return sync
