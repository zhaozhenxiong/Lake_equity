"""Frame helpers for the one polars behaviour this codebase keeps tripping on.

``pl.DataFrame()`` — no columns, no rows — is the ordinary "nothing to report"
value across the adapters and steps. Polars 1.x broadcasts a literal expression
against a **zero-column** frame to length one, so::

    pl.DataFrame().with_columns(pl.lit(None).alias("source"))   # -> 1 row on 1.x!

Polars 2 keeps zero rows here, but the declared ``polars>=1.0`` still admits
1.x, so the guard stays.

That is not the same as a zero-*row* frame that has a schema, which behaves as
expected and keeps zero rows. The difference is invisible at the call site and
the resulting row is quietly destructive: it carries whatever literals were
being stamped and nothing else, so it has no primary key. A strict
``validate_dataframe`` rejects it — but a ``pl.concat(..., how="diagonal_*")``
with a real day's rows runs first in several paths, fills its missing keys with
nulls, and launders it into the lake.

Found in ``step_trading_status`` (2026-08-30), where a session whose vendor
universe was empty produced a row with no symbol and no ``trade_date``, which
then failed the *next* day's date validation and pointed at the wrong session.

Use :func:`with_columns_unless_blank` wherever the frame being stamped can be
the untyped empty value. Where a frame is built with an explicit schema, plain
``with_columns`` is fine and clearer.
"""

from __future__ import annotations

import polars as pl


def is_blank(df: pl.DataFrame) -> bool:
    """Whether *df* carries no columns at all — the untyped "nothing" value.

    Distinct from :meth:`polars.DataFrame.is_empty`, which is also true for a
    frame that has a schema and no rows. Only the column-less case broadcasts.
    """
    return not df.width


def with_columns_unless_blank(df: pl.DataFrame, *exprs, **named) -> pl.DataFrame:
    """``df.with_columns(...)`` that cannot conjure a row from nothing."""
    if is_blank(df):
        return df
    return df.with_columns(*exprs, **named)
