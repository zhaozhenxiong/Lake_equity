"""Bounded summaries of canonical corrections, attached to revision receipts."""

from __future__ import annotations

import polars as pl

from cnequity.domain.canonical import dedupe_by_primary_key
from cnequity.domain.schemas import PRIMARY_KEYS


def summarize_changes(before: pl.DataFrame, after: pl.DataFrame, dataset: str) -> dict:
    """Count changed keys and source transitions without duplicating lake rows.

    Full values remain in the parent and published generations. Observation
    timestamp churn alone is not a data correction. Comparisons use exact
    structs rather than a lossy row hash.
    """
    keys = PRIMARY_KEYS[dataset]
    before = dedupe_by_primary_key(before, dataset)
    after = dedupe_by_primary_key(after, dataset)
    columns = sorted((set(before.columns) | set(after.columns)) - {"fetched_at", "observed_at"})
    # Old optional columns may be absent on one side of a lazy migration.
    before_count = before.height
    aligned = pl.concat([before, after], how="diagonal_relaxed")
    before, after = aligned.slice(0, before_count), aligned.slice(before_count)
    old = before.select(
        *keys, pl.struct(columns).alias("__before"), pl.col("source").alias("__source_before")
    )
    new = after.select(
        *keys, pl.struct(columns).alias("__after"), pl.col("source").alias("__source_after")
    )
    joined = new.join(old, on=keys, how="left", validate="1:1")
    inserted = joined.filter(pl.col("__before").is_null())
    updated = joined.filter(
        pl.col("__before").is_not_null() & pl.col("__before").ne_missing(pl.col("__after"))
    )
    transitions = (
        updated.filter(pl.col("__source_before").ne_missing(pl.col("__source_after")))
        .group_by("__source_before", "__source_after")
        .len()
        .sort("__source_before", "__source_after")
    )
    return {
        "inserted_keys": inserted.height,
        "updated_keys": updated.height,
        "source_changes": [
            {"from": row["__source_before"], "to": row["__source_after"], "keys": row["len"]}
            for row in transitions.iter_rows(named=True)
        ],
        "updated_key_sample": [
            {key: str(value) for key, value in row.items()}
            for row in updated.select(keys).sort(keys).head(10).to_dicts()
        ],
    }
