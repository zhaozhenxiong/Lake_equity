"""How much a corporate-action row's evidence is worth when two fetches collide.

A later fetch of the same event key normally supersedes an older one.  That is
wrong when the older row carries evidence the newer one lacks: a vendor sweep
without a payment date, or one that restates an amount an issuer notice
already settled, must not win on recency alone.  The canonical merge therefore
orders corporate actions by this evidence level before ``fetched_at``:

* 2 — settled by the issuer's own notice (``payment_source`` starting with
  ``issuer_notice:``, or one of the reviewed stock terms below);
* 1 — a vendor-reported payment date (e.g. ``baostock:dividPayDate``);
* 0 — no payment evidence.

Evidence is valid only if the cash is paid on or after the ex-date.  China
Clearing credits A-share cash on the ex-date, so an earlier date is an error in
the source (issuer PDFs copy last year's template and keep its year) and counts
as no evidence at all.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from cnequity.domain.symbols import is_all_a_symbol, is_cdr_symbol

ISSUER_NOTICE_PREFIX = "issuer_notice:"
PAYMENT_EVIDENCE_INPUTS = frozenset(
    {"symbol", "ex_date", "action_type", "payment_date", "payment_source"}
)
REVIEWED_TERMS_TOLERANCE = 1e-8

# BJ issuer notices that state a capital-reserve transfer only, while TDX also
# reported the same quantity as a bonus.  (symbol, ex_date) -> (notice id,
# PDF sha256, per-share transfer).  The adapter re-verifies each PDF on use.
REVIEWED_BJ_TRANSFERS: dict[tuple[str, date], tuple[str, str, float]] = {
    ("920405.BJ", date(2022, 5, 23)): (
        "AN202205131565353041",
        "706418570422674e27c39e9a7a1c9e56d43d697ac0ba8d1b8fbe2cd82928ca85",
        1.0,
    ),
    ("920395.BJ", date(2023, 4, 19)): (
        "AN202304121585377832",
        "e8f6d811eccaca3f19aae4cbce5fac198a061b10c166831a81acdc42cd299d7f",
        1.0,
    ),
    ("920533.BJ", date(2023, 5, 31)): (
        "AN202305231587102362",
        "43b8c16015fd08bf2203654a609c82a46537ca5a759f9e3b85fc3c614be409cf",
        0.8,
    ),
    ("920014.BJ", date(2023, 7, 3)): (
        "AN202306261591302895",
        "43734eaf02049b67ef7efe1b7a5060116a7875415ff733a85a4d2658303f7a95",
        0.3,
    ),
    ("920395.BJ", date(2024, 4, 17)): (
        "AN202404101630109809",
        "c0f735400712a7374766f8c703e4f192e6c012ba27472b2c7b0a38d4f0d07d38",
        0.4,
    ),
    ("920926.BJ", date(2024, 6, 5)): (
        "AN202405291634819265",
        "07558d86034408b7467fa65420e1558661d97a6f5063ff4521d2e870daa4a27d",
        0.45,
    ),
}

# 920799.BJ: a correction notice changed only the dilution reference values;
# the final notice pays 0.25 cash and transfers 0.5 share per share.
REVIEWED_BJ_CORRECTION_CHAIN = {
    "symbol": "920799.BJ",
    "ex_date": date(2022, 6, 1),
    "transfer_ratio": 0.5,
    "correction": (
        "AN202205271568235538",
        "aff29c9ff01b3bf891d19db9ad3edf1599f1900dec76f0fec6d7630c93da0399",
    ),
    "final": (
        "AN202205271568235539",
        "5c121fd9693a80f17fa4ac5f3d0ea19bfabc0c66fbe7c2b5045f2457487483f6",
    ),
}


def reviewed_stock_terms() -> dict[tuple[str, date, str], tuple[str, float]]:
    """(symbol, ex_date, action_type) -> (ratio column, issuer-stated value)."""
    terms: dict[tuple[str, date, str], tuple[str, float]] = {}
    for (symbol, ex_date), (_notice, _digest, transfer) in REVIEWED_BJ_TRANSFERS.items():
        terms[(symbol, ex_date, "bonus")] = ("bonus_ratio", 0.0)
        terms[(symbol, ex_date, "transfer")] = ("transfer_ratio", transfer)
    chain = REVIEWED_BJ_CORRECTION_CHAIN
    terms[(chain["symbol"], chain["ex_date"], "bonus")] = ("bonus_ratio", 0.0)
    terms[(chain["symbol"], chain["ex_date"], "transfer")] = (
        "transfer_ratio",
        chain["transfer_ratio"],
    )
    return terms


def ex_date_rule_applies(symbol: str) -> bool:
    """Whether China Clearing's ex-date cash crediting covers this code.

    True for exchange-listed A-share stocks, where a missing payment date can
    fall back on the ex-date: of 27,000+ reported dates for 2016-2024 events,
    all but two fall on the ex-date and those two one day after it.  Funds,
    CDRs and B shares pay on their own schedules and need a reported date.
    """
    code, _, exchange = symbol.partition(".")
    return is_all_a_symbol(code, exchange) and not is_cdr_symbol(code, exchange)


def valid_payment_expr() -> pl.Expr:
    return pl.col("payment_date").is_not_null() & (pl.col("payment_date") >= pl.col("ex_date"))


def clear_invalid_payment_evidence(df: pl.DataFrame) -> pl.DataFrame:
    """Drop a payment date that precedes its ex-date, with its source label."""
    if not {"payment_date", "payment_source", "ex_date"} <= set(df.columns):
        return df
    invalid = pl.col("payment_date").is_not_null() & ~valid_payment_expr()
    return df.with_columns(
        pl.when(invalid).then(None).otherwise(pl.col(c)).alias(c)
        for c in ("payment_date", "payment_source")
    )


def evidence_level_expr(columns: set[str]) -> pl.Expr | None:
    """0/1/2 evidence level for corporate-action rows (see module docstring)."""
    if not PAYMENT_EVIDENCE_INPUTS <= columns:
        return None
    source = pl.col("payment_source").fill_null("")
    valid = valid_payment_expr()
    reviewed = pl.lit(False)
    for (symbol, ex_date, action), (column, value) in reviewed_stock_terms().items():
        if column not in columns:
            continue
        reviewed = reviewed | (
            (pl.col("symbol") == symbol)
            & (pl.col("ex_date") == ex_date)
            & (pl.col("action_type") == action)
            & ((pl.col(column).cast(pl.Float64) - value).abs() <= REVIEWED_TERMS_TOLERANCE)
        )
    return (
        pl.when((valid & source.str.starts_with(ISSUER_NOTICE_PREFIX)) | reviewed)
        .then(2)
        .when(valid & (source != ""))
        .then(1)
        .otherwise(0)
    )


def evidence_level_sql(columns: set[str]) -> str | None:
    """SQL counterpart of ``evidence_level_expr``, using the same reviewed terms."""
    if not PAYMENT_EVIDENCE_INPUTS <= columns:
        return None

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    reviewed = []
    for (symbol, ex_date, action), (column, value) in reviewed_stock_terms().items():
        if column in columns:
            reviewed.append(
                f"(symbol = {literal(symbol)} AND ex_date = DATE '{ex_date.isoformat()}' "
                f"AND action_type = {literal(action)} "
                f"AND abs(CAST({column} AS DOUBLE) - {value!r}) <= {REVIEWED_TERMS_TOLERANCE})"
            )
    valid = "(payment_date IS NOT NULL AND payment_date >= ex_date)"
    source = "coalesce(payment_source, '')"
    terms = " OR ".join(reviewed) or "FALSE"
    return (
        f"CASE WHEN ({valid} AND starts_with({source}, {literal(ISSUER_NOTICE_PREFIX)})) "
        f"OR ({terms}) THEN 2 WHEN {valid} AND {source} <> '' THEN 1 ELSE 0 END"
    )
