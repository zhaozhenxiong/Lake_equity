"""Map a corporate action to the first session its security trades afterward."""

from __future__ import annotations

import polars as pl


def effective_session(
    actions: pl.DataFrame,
    sessions: pl.DataFrame,
    *,
    date_col: str = "ex_date",
    session_col: str = "trade_date",
) -> pl.DataFrame:
    """Add ``effective_session`` to actions, preserving the stated ex-date.

    A halt or market closure can separate the stated date from the first
    tradable session. An event after the last known session has a null result;
    callers decide whether to retain it. Sessions must belong to the same
    security as the action.
    """
    traded = sessions.select("symbol", pl.col(session_col).alias("effective_session")).unique()
    return actions.sort("symbol", date_col).join_asof(
        traded.sort("symbol", "effective_session"),
        left_on=date_col,
        right_on="effective_session",
        by="symbol",
        strategy="forward",
        check_sortedness=False,
    )
