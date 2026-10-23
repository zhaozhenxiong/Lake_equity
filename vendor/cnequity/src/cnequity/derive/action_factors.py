"""Back-adjusted factors computed from the lake's own corporate actions.

For a market no vendor adjusts completely (Beijing: Sina's factor misses
half the cash dividends and every one before 2020), the factor is derived
from first-hand facts instead — the recorded actions and the previous close
— by the exchange's own ex-rights rule:

    reference = (prev_close - cash + allotment_ratio * allotment_price)
                / (1 + bonus + transfer + allotment_ratio)
    step      = prev_close / reference

A restructuring conversion publishes its reference price, and that price is
used as given. Each action takes effect on the security's first traded
session on or after its ex-date. The series is 1.0 at the first bar, the
level convention of the stored hfq factor.
"""

from __future__ import annotations

import polars as pl

from cnequity.domain.action_sessions import effective_session

SOURCE = "derived_actions"


def action_factor_series(
    bars: pl.DataFrame, terms: pl.DataFrame
) -> tuple[pl.DataFrame, list[dict]]:
    """``(trade_date, factor)`` rows for one security, and the events it skipped.

    ``bars`` needs ``trade_date, close`` for the security's traded sessions.
    ``terms`` is one row per ex-date as ``_action_terms`` builds it
    (``ex_date, _dividend, _bonus, _transfer, _allotment, _allot_cash,
    _split`` and optionally ``_reference``). A reference price at or below
    zero cannot be priced; that event is skipped and reported, not guessed.
    """
    empty = pl.DataFrame(schema={"trade_date": pl.Date, "factor": pl.Float64})
    closes = bars.select("trade_date", "close").drop_nulls().sort("trade_date")
    if closes.is_empty():
        return empty, []
    first = closes.get_column("trade_date")[0]
    base = pl.DataFrame({"trade_date": [first], "factor": [1.0]})
    if terms.is_empty():
        return base, []
    sessions = closes.with_columns(
        pl.lit("_").alias("symbol"),
        pl.col("close").shift(1).alias("_prev_close"),
    )
    if "_reference" not in terms.columns:
        terms = terms.with_columns(pl.lit(None, dtype=pl.Float64).alias("_reference"))
    events = (
        effective_session(
            terms.with_columns(pl.lit("_").alias("symbol")),
            sessions.select("symbol", "trade_date"),
        )
        .filter(pl.col("effective_session").is_not_null())
        # An action on the first bar has no close before it to step from.
        .filter(pl.col("effective_session") > first)
        .group_by("effective_session")
        .agg(
            *[
                pl.col(c).fill_null(0.0).sum()
                for c in ("_dividend", "_bonus", "_transfer", "_allotment", "_allot_cash")
            ],
            pl.col("_split").fill_null(1.0).product(),
            pl.col("_reference").max(),
            pl.col("ex_date").min().alias("ex_date"),
        )
        .join(
            sessions.select(pl.col("trade_date").alias("effective_session"), "_prev_close"),
            on="effective_session",
        )
        .with_columns(
            pl.when(pl.col("_reference").is_not_null())
            .then(pl.col("_reference"))
            .otherwise(
                (pl.col("_prev_close") - pl.col("_dividend") + pl.col("_allot_cash"))
                / (
                    (1.0 + pl.col("_bonus") + pl.col("_transfer") + pl.col("_allotment"))
                    * pl.col("_split")
                )
            )
            .alias("_ref_price")
        )
        .sort("effective_session")
    )
    unpriced = events.filter(~(pl.col("_ref_price") > 0) | ~(pl.col("_prev_close") > 0))
    skipped = [
        {"ex_date": str(r["ex_date"]), "effective_session": str(r["effective_session"])}
        for r in unpriced.iter_rows(named=True)
    ]
    priced = events.join(unpriced.select("effective_session"), on="effective_session", how="anti")
    if priced.is_empty():
        return base, skipped
    steps = priced.select(
        pl.col("effective_session").alias("trade_date"),
        (pl.col("_prev_close") / pl.col("_ref_price")).cum_prod().alias("factor"),
    )
    return pl.concat([base, steps], how="vertical_relaxed"), skipped
