"""Contract metadata from observed bars and authoritative exchange references.

Observation bounds are not listing or expiry dates: a missing contract can be
an ingestion failure. Only references populate contractual dates; observations
remain available as first_seen_date and last_seen_date. Legacy observed basis
labels describe the observation window, never proof of expiry.
"""

from __future__ import annotations

import polars as pl

from cnequity.domain.derivatives import parse_future_code, parse_option_code
from cnequity.domain.futures_products import product_spec

__all__ = ["build_futures_contracts", "build_option_contracts", "observed_contracts"]

_OBSERVED_COLUMNS = (
    "symbol",
    "source",
    "exchange",
    "exchange_code",
    "product",
    "first_seen_date",
    "last_seen_date",
)


def observed_contracts(bars: pl.LazyFrame, *, kind: str) -> pl.DataFrame:
    """First and last session of every contract, plus each exchange's span."""
    extra = ["underlying_symbol", "option_type", "strike"] if kind == "option" else []
    if "source" not in bars.collect_schema().names():
        bars = bars.with_columns(pl.lit("futures_exchange").alias("source"))
    per_symbol = (
        bars.group_by("symbol")
        .agg(
            pl.col("exchange").last(),
            pl.col("exchange_code").sort_by("trade_date").last(),
            pl.col("product").last(),
            pl.col("trade_date").min().alias("first_seen_date"),
            pl.col("trade_date").max().alias("last_seen_date"),
            pl.col("source").sort_by("trade_date").last(),
            *[pl.col(c).sort_by("trade_date").last() for c in extra],
        )
        .collect()
    )
    spans = (
        bars.group_by("exchange")
        .agg(
            pl.col("trade_date").min().alias("_exchange_first"),
            pl.col("trade_date").max().alias("_exchange_last"),
        )
        .collect()
    )
    return per_symbol.join(spans, on="exchange", how="left")


def _dated(observed: pl.DataFrame, reference: pl.DataFrame | None) -> pl.DataFrame:
    if reference is not None and not reference.is_empty():
        ref = (
            reference.select(
                "symbol",
                pl.col("list_date").alias("_ref_list"),
                pl.col("last_trade_date").alias("_ref_last"),
            )
            .group_by("symbol", maintain_order=True)
            .agg(
                pl.col("_ref_list").drop_nulls().last(),
                pl.col("_ref_last").drop_nulls().last(),
            )
        )
        frame = observed.join(ref, on="symbol", how="left")
    else:
        frame = observed.with_columns(
            pl.lit(None, dtype=pl.Date).alias("_ref_list"),
            pl.lit(None, dtype=pl.Date).alias("_ref_last"),
        )
    expired = pl.col("last_seen_date") < pl.col("_exchange_last")
    truncated = pl.col("first_seen_date") <= pl.col("_exchange_first")
    has_ref = pl.col("_ref_last").is_not_null() | pl.col("_ref_list").is_not_null()
    return frame.with_columns(
        pl.when(has_ref & pl.col("_ref_list").is_not_null())
        .then(pl.col("_ref_list"))
        .otherwise(pl.lit(None, dtype=pl.Date))
        .alias("list_date"),
        pl.when(has_ref)
        .then(pl.col("_ref_last"))
        .otherwise(pl.lit(None, dtype=pl.Date))
        .alias("_last"),
        pl.when(has_ref)
        .then(pl.lit("exchange"))
        .when(~expired)
        .then(pl.lit("open"))
        .when(pl.col("source") == "sina")
        .then(pl.lit("vendor_observed"))
        .when(truncated)
        .then(pl.lit("observed_truncated"))
        .otherwise(pl.lit("observed"))
        .alias("dates_basis"),
    )


def _spec_columns(rows: list[dict], kind: str) -> list[dict]:
    parse = parse_option_code if kind == "option" else parse_future_code
    for row in rows:
        contract = parse(row["exchange_code"], row["exchange"], row["first_seen_date"])
        series = contract.series if kind == "option" else contract
        spec = product_spec(
            row["exchange"],
            row["product"],
            kind,
            delivery=series.delivery_month,
            on=row["first_seen_date"],
        )
        row["product_name"] = spec.name if spec else None
        row["multiplier"] = float(spec.multiplier) if spec else None
        row["tick_size"] = float(spec.tick_size) if spec else None
        if kind == "option":
            row["expiry_month"] = series.delivery_month
            row["underlying_kind"] = contract.underlying_kind
            row["exercise_style"] = spec.exercise_style if spec else None
        else:
            row["delivery_month"] = series.delivery_month
            row["quote_unit"] = spec.quote_unit if spec else None
    return rows


def build_futures_contracts(
    bars: pl.LazyFrame, reference: pl.DataFrame | None = None
) -> pl.DataFrame:
    observed = observed_contracts(bars, kind="future")
    if observed.is_empty():
        return pl.DataFrame()
    dated = _dated(observed, reference).rename({"_last": "last_trade_date"})
    rows = _spec_columns(
        dated.select(*_OBSERVED_COLUMNS, "list_date", "last_trade_date", "dates_basis").to_dicts(),
        "future",
    )
    return pl.DataFrame(rows, infer_schema_length=None)


def build_option_contracts(
    bars: pl.LazyFrame, reference: pl.DataFrame | None = None
) -> pl.DataFrame:
    observed = observed_contracts(bars, kind="option")
    if observed.is_empty():
        return pl.DataFrame()
    dated = _dated(observed, reference).rename({"_last": "expiry_date"})
    rows = _spec_columns(
        dated.select(
            *_OBSERVED_COLUMNS,
            "underlying_symbol",
            "option_type",
            "strike",
            "list_date",
            "expiry_date",
            "dates_basis",
        ).to_dicts(),
        "option",
    )
    return pl.DataFrame(rows, infer_schema_length=None)
