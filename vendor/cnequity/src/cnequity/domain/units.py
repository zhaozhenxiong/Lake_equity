"""The lake's traded-quantity unit, and the per-vendor conversions into it.

A-share vendors disagree about what a "volume" is. Some report 股 (shares),
some report 手 (lots, 100 shares). Nothing in a bar payload says which, so a
column that mixes both is silently wrong by exactly 100× — large enough to
destroy any turnover or liquidity factor, small enough that OHLC checks and
row counts never notice.

**The lake stores 股.** That is what ``docs/zh/datasets/schema.md`` has always
promised, and it is the only choice that makes ``amount ≈ close × volume``
hold, which in turn is what lets :mod:`cnequity.quality.unit_checks`
detect a regression from the data alone.

Every adapter converts at its own boundary, before the row is handed on, so a
frame that has left an adapter is always in 股.

Measured units per vendor (ratio = ``amount / close / volume`` over the whole
curated lake, which is ~1 when volume is 股 and ~100 when it is 手):

===============  =========  =====================================
vendor           native     evidence
===============  =========  =====================================
tdx_protocol     手         median 100.000 over 12,182,204 rows
ths              股         median 0.999 over 5,303,037 rows
baostock         股         median 1.000 over 374,888 rows
sina             股         vendor docs; ``amount`` is not served,
                            so the ratio cannot be measured
eastmoney        手         inferred — see the caveat below
===============  =========  =====================================

The EastMoney reading is **not independently verified**: ``push2his`` is
unreachable from the network this was measured on, and the only EastMoney rows
in the lake are all-zero suspension placeholders. It is taken from the same
endpoint and field index that ``commodity_bars`` already documents as 东财口径
手 (``docs/zh/datasets/schema.md``). If it is wrong, ``daily_bars_volume_unit``
fires the first time a real EastMoney row is curated, which is the point of
that check.

TDX is per-frequency, not per-vendor: daily K (``frequency=9``) is 手, while
1-minute bars off the same wire parser are 股 (verified: 600519 1m bar
vol=59,700 against amount=88,977,784 at ~1490 → 59,716 shares). A future
``minute_bars`` dataset must not reuse the daily conversion, and any
minute-to-daily volume reconciliation has to compare 股 to 股.
"""

from __future__ import annotations

import polars as pl

__all__ = ["SHARES_PER_LOT", "lots_to_shares", "turnover_defect_expr"]

SHARES_PER_LOT = 100

# A traded bar's average price, amount / volume, off the day's range by this
# factor is a unit fault (手 against 股 is 100×), not block or after-hours
# trades priced a few percent away from the auction.
_UNIT_FAULT_FACTOR = 2.0


def lots_to_shares(volume_lots: float | int | None) -> int:
    """Convert a vendor's 手 quantity to the lake's 股.

    ``None`` becomes 0 to match the suspension convention (``volume=0``,
    ``amount=0``) rather than introducing a null into an ``Int64`` column.
    """
    if volume_lots is None:
        return 0
    return int(volume_lots) * SHARES_PER_LOT


def turnover_defect_expr() -> pl.Expr:
    """Why a daily bar's turnover cannot be used, or null when it can.

    ``missing_amount`` — the source published no turnover for a traded bar.
    ``zero_amount`` — a traded bar with zero turnover: a missing value stored
    as 0 (同花顺 pre-2004 year files). ``unit_mismatch`` — the average price
    is off the day's range by more than 2×, so volume and amount are not on
    the same unit. A suspension (volume 0, amount 0) is not a defect.
    """
    traded = pl.col("volume") > 0
    average = pl.col("amount") / pl.col("volume")
    return (
        pl.when(traded & pl.col("amount").is_null())
        .then(pl.lit("missing_amount"))
        .when(traded & (pl.col("amount") <= 0))
        .then(pl.lit("zero_amount"))
        .when(
            traded
            & (
                (average > pl.col("high") * _UNIT_FAULT_FACTOR)
                | (average < pl.col("low") / _UNIT_FAULT_FACTOR)
            )
        )
        .then(pl.lit("unit_mismatch"))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
    )
