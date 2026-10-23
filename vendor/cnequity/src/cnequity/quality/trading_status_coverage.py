"""Trading-state coverage of actual prints, independent of historical ST receipts."""

from __future__ import annotations

from datetime import date

import polars as pl

from cnequity.config import Config
from cnequity.domain.canonical import dedupe_lazy_by_primary_key
from cnequity.domain.market_profile import exchange_start
from cnequity.domain.trading_status import DERIVED_TRADED_SOURCE, risk_warning_value_expr
from cnequity.query.parquet_scan import dataset_has_parquet, scan_parquet_root
from cnequity.storage.read_context import ReadContext, read_root

KEY = ["symbol", "trade_date"]


def traded_status_evidence(
    config: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    read_context: ReadContext | None = None,
) -> pl.LazyFrame:
    """One canonical positive-volume bar per key, with listing and status evidence.

    Null/absent volume is unknown, not proof of a print. Unknown listing dates,
    missing instruments and dates outside the listed life remain visible as
    exclusions, and never become derived trading rows.
    """
    context = read_context or ReadContext.capture(
        config, ("daily_bars", "instruments", "trading_status"), {}
    )
    bars_root = read_root(config, "daily_bars", context)
    if not dataset_has_parquet(bars_root, committed=False):
        return pl.LazyFrame(
            schema={
                "symbol": pl.String,
                "trade_date": pl.Date,
                "asset_class": pl.String,
                "exclusion": pl.String,
                "has_status": pl.Boolean,
                "derived_traded": pl.Boolean,
                "risk_warning_known": pl.Boolean,
                "contradiction": pl.Boolean,
            }
        )
    bars = scan_parquet_root(
        bars_root, partition_col="trade_date", start=start, end=end, committed=False
    )
    if "volume" not in bars.collect_schema():
        bars = bars.with_columns(pl.lit(None, dtype=pl.Int64).alias("volume"))
    bars = dedupe_lazy_by_primary_key(bars, "daily_bars").filter(pl.col("volume") > 0)
    bars = bars.select(KEY)
    inst_root = read_root(config, "instruments", context)
    if dataset_has_parquet(inst_root, committed=False):
        inst = scan_parquet_root(inst_root, hive=False, committed=False)
        for column, dtype in (
            ("asset_type", pl.String),
            ("list_date", pl.Date),
            ("delist_date", pl.Date),
        ):
            if column not in inst.collect_schema():
                inst = inst.with_columns(pl.lit(None, dtype=dtype).alias(column))
        inst = dedupe_lazy_by_primary_key(inst, "instruments").select(
            "symbol", "asset_type", "list_date", "delist_date"
        )
        inst = inst.with_columns(
            pl.when(pl.col("symbol").str.ends_with(".BJ") & pl.col("list_date").is_not_null())
            .then(exchange_start(pl.col("list_date")))
            .otherwise(pl.col("list_date"))
            .alias("list_date")
        )
    else:
        inst = pl.LazyFrame(
            schema={
                "symbol": pl.String,
                "asset_type": pl.String,
                "list_date": pl.Date,
                "delist_date": pl.Date,
            }
        )
    rows = bars.join(inst, on="symbol", how="left").with_columns(
        pl.when(pl.col("asset_type") == "etf")
        .then(pl.lit("etf"))
        .when((pl.col("asset_type") == "stock") & pl.col("symbol").str.ends_with(".BJ"))
        .then(pl.lit("bse_stock"))
        .when(pl.col("asset_type") == "stock")
        .then(pl.lit("sh_sz_stock"))
        .otherwise(pl.lit("other_or_unknown"))
        .alias("asset_class"),
        pl.when(~pl.col("asset_type").is_in(["stock", "etf"]).fill_null(False))
        .then(pl.lit("unsupported_or_missing_instrument"))
        .when(pl.col("list_date").is_null())
        .then(pl.lit("unknown_listing_date"))
        .when(pl.col("trade_date") < pl.col("list_date"))
        .then(pl.lit("before_listing"))
        .when(pl.col("trade_date") >= pl.col("delist_date"))
        .then(pl.lit("on_or_after_delisting"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("exclusion"),
    )
    status_root = read_root(config, "trading_status", context)
    if dataset_has_parquet(status_root, committed=False):
        status = scan_parquet_root(
            status_root, partition_col="trade_date", start=start, end=end, committed=False
        )
        columns = status.collect_schema().names()
        status = dedupe_lazy_by_primary_key(status, "trading_status").select(
            *KEY,
            pl.lit(True).alias("has_status"),
            (pl.col("source") == DERIVED_TRADED_SOURCE).alias("derived_traded"),
            risk_warning_value_expr(columns).is_not_null().alias("risk_warning_known"),
            (~pl.col("is_trading") | pl.col("status").is_in(["suspended", "delisted"])).alias(
                "contradiction"
            ),
        )
    else:
        status = pl.LazyFrame(
            schema={
                "symbol": pl.String,
                "trade_date": pl.Date,
                "has_status": pl.Boolean,
                "derived_traded": pl.Boolean,
                "risk_warning_known": pl.Boolean,
                "contradiction": pl.Boolean,
            }
        )
    return rows.join(status, on=KEY, how="left").select(
        *KEY,
        "asset_class",
        "exclusion",
        pl.col("has_status", "derived_traded", "risk_warning_known", "contradiction").fill_null(
            False
        ),
    )


def trading_state_coverage_report(
    config: Config, *, start: date | None = None, end: date | None = None
) -> dict:
    """Count all printed keys, including history before a symbol's first status."""
    evidence = traded_status_evidence(config, start=start, end=end)
    eligible = evidence.filter(pl.col("exclusion").is_null())
    groups = (
        eligible.group_by("asset_class", pl.col("trade_date").dt.strftime("%Y-%m").alias("month"))
        .agg(
            pl.len().alias("traded_rows"),
            (~pl.col("has_status")).sum().alias("missing_rows"),
            pl.col("derived_traded").sum().alias("derived_traded_rows"),
            (~pl.col("risk_warning_known")).sum().alias("risk_warning_unknown_rows"),
            pl.col("contradiction").sum().alias("contradictory_rows"),
        )
        .sort("asset_class", "month")
        .collect()
    )
    exclusions = evidence.filter(pl.col("exclusion").is_not_null()).group_by("exclusion")
    excluded = dict(exclusions.len().sort("exclusion").collect().iter_rows())
    counts = {
        name: int(groups[name].sum() or 0)
        for name in groups.columns
        if name not in {"asset_class", "month"}
    }
    return {
        **counts,
        "excluded_rows": sum(excluded.values()),
        "exclusions": excluded,
        "by_class_month": groups.to_dicts(),
        "note": "Positive-volume stock/ETF keys only; this is not an ST coverage receipt.",
    }


def trading_state_coverage_findings(config: Config, end: date) -> list[dict]:
    report = trading_state_coverage_report(config, end=end)
    if not report["traded_rows"] and not report["excluded_rows"]:
        return []
    missing = report["missing_rows"]
    return [
        {
            "dataset": "trading_status",
            "check": "trading_state_historical_coverage",
            "severity": "warning"
            if missing or report["excluded_rows"] or report["contradictory_rows"]
            else "info",
            "message": f"{missing} traded stock/ETF key(s) lack status, including pre-inception history; "
            f"{report['risk_warning_unknown_rows']} lack ST evidence"
            + (
                f"; {report['excluded_rows']} key(s) are left out of the count ("
                + ", ".join(f"{name}: {n}" for name, n in report["exclusions"].items())
                + ")"
                if report["excluded_rows"]
                else ""
            ),
            **report,
        }
    ]
