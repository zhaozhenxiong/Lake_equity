"""Original-disclosure reconstructions, kept separate from observed vintages."""

import polars as pl

from cnequity.domain.pit import normalize_pit_storage_columns
from cnequity.domain.schemas import PRIMARY_KEYS

DATASET = "financial_statement_items"
PERIOD_KEYS = ["symbol", "report_period"]
FACT_KEYS = [*PERIOD_KEYS, "statement_type", "item_code"]


def original_disclosures(frame: pl.DataFrame | pl.LazyFrame) -> pl.DataFrame | pl.LazyFrame:
    """LICO summary items supply dates, never the publication time of a restatement."""
    return (
        frame.filter(
            (
                (pl.col("statement_type") == "indicator")
                | (
                    (pl.col("statement_type") == "income")
                    & pl.col("item_code").is_in(["revenue", "net_profit"])
                )
            )
            & pl.col("source").is_in(["eastmoney", "eastmoney_backfill"])
        )
        .group_by(PERIOD_KEYS)
        .agg(pl.col("announce_date").min().alias("original_date"))
    )


def reconstruct_disclosures(frame: pl.DataFrame, dates: pl.DataFrame) -> pl.DataFrame:
    """Fill absent original-date keys with explicitly inexact, first observed values.

    Never overwrite an existing original-date vintage, even from another source.
    Source publication and availability of the original *value* are unknown;
    observation time stays honest and strict PIT excludes these reconstructions.
    """
    if frame.is_empty() or dates.is_empty():
        return frame.clear()
    matched = frame.join(dates, on=PERIOD_KEYS)
    existing = (
        matched.filter(pl.col("announce_date") == pl.col("original_date"))
        .select(FACT_KEYS)
        .unique()
    )
    candidates = (
        matched.filter(
            pl.col("source").is_in(["eastmoney", "eastmoney_backfill"])
            & (pl.col("announce_date") > pl.col("original_date"))
        )
        .join(existing, on=FACT_KEYS, how="anti")
        .sort("fetched_at" if "fetched_at" in frame.columns else "announce_date")
        .unique(subset=FACT_KEYS, keep="first", maintain_order=True)
        .with_columns(
            pl.col("original_date").alias("announce_date"),
            pl.lit("eastmoney_backfill").alias("source"),
            pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("available_at"),
            pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("source_published_at"),
        )
        .drop("original_date", "revision_id", strict=False)
    )
    if "fetched_at" in candidates.columns:
        candidates = normalize_pit_storage_columns(candidates, DATASET)
    return candidates.unique(subset=PRIMARY_KEYS[DATASET], keep="first")
