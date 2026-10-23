"""Read-only research-window evidence; never certify a full market universe."""

from datetime import date, timedelta

import polars as pl

from cnequity.adapters.futures_exchange import members
from cnequity.adapters.futures_exchange.registry import enabled_exchanges, reader
from cnequity.domain.derivatives import UNPUBLISHED_FUTURES_SESSIONS
from cnequity.quality.derivative_checks import (
    DERIVATIVE_BAR_CONTRACTS,
    _last_trade_dates,
    _scan,
    exchange_session_gaps,
)
from cnequity.query.calendar import list_trading_dates
from cnequity.storage.derivative_evidence import owed_sessions


def derivative_window(config, dataset: str, start: date, end: date) -> dict:
    """Check exchange days, adjacent known contracts, metadata and receipt debt.

    The first day can use the preceding session outside the requested window.
    A missing previous session is not replaced by an older observation. Unknown
    expiry remains conservative evidence, not proof of an active universe.
    """
    if dataset not in DERIVATIVE_BAR_CONTRACTS:
        raise ValueError("dataset must be futures_bars or option_bars")
    if start > end:
        raise ValueError("start must not exceed end")
    kind = "futures" if dataset == "futures_bars" else "options"
    routes = enabled_exchanges(config)
    sessions = list_trading_dates(config, start - timedelta(days=40), end)
    wanted = [day for day in sessions if day >= start]
    bars = _scan(config, dataset)
    pairs = (
        bars.filter(pl.col("trade_date").is_in(sessions))
        .select("trade_date", "exchange", "product", "symbol")
        .unique()
        .collect()
        if bars is not None
        else pl.DataFrame(
            schema={
                "trade_date": pl.Date,
                "exchange": pl.String,
                "product": pl.String,
                "symbol": pl.String,
            }
        )
    )
    included = {name for publisher in routes for name in members(publisher)}
    pairs = pairs.filter(pl.col("exchange").is_in(sorted(included)))
    window = pairs.filter(pl.col("trade_date") >= start)
    table, end_column = DERIVATIVE_BAR_CONTRACTS[dataset]
    contracts = _scan(config, table)
    metadata = contracts.collect() if contracts is not None else pl.DataFrame()
    known = set(metadata["symbol"].to_list()) if "symbol" in metadata.columns else set()
    observed = set(window["symbol"].to_list())
    ends = _last_trade_dates(config, dataset)
    by_day = {
        (day, exchange): set(group["symbol"].to_list())
        for (day, exchange), group in pairs.group_by("trade_date", "exchange")
    }
    authoritative = {}
    if not metadata.is_empty():
        for row in metadata.filter(pl.col("dates_basis") == "exchange").iter_rows(named=True):
            if row.get("list_date") and row.get(end_column):
                authoritative.setdefault(row["exchange"], []).append(
                    (row["symbol"], row["list_date"], row[end_column])
                )
    missing = []
    unverified = ["No complete historical listed-contract universe; no-gap results are unverified."]
    for publisher in routes:
        floor = reader(config, publisher).first_session(kind)
        if floor is None:
            unverified.append(f"{publisher}: configured route does not supply {kind}")
            continue
        if publisher == "DCE" and config.futures_dce_route == "sina":
            unverified.append(
                "DCE: Sina omits zero-trade contracts; contract absence is not proven"
            )
            continue
        for exchange in members(publisher):
            if exchange == "INE":
                unverified.append(
                    "INE: leading coverage boundary lacks an independent dated universe"
                )
            excused = UNPUBLISHED_FUTURES_SESSIONS.get(exchange, ()) if kind == "futures" else ()
            for index, day in enumerate(sessions):
                if day < max(start, floor) or day in excused:
                    continue
                previous = sessions[index - 1] if index else None
                expected = {
                    symbol
                    for symbol in by_day.get((previous, exchange), set())
                    if ends.get(symbol, date.max) >= day
                }
                # Authoritative dates also cover contracts absent throughout the window.
                expected.update(
                    symbol
                    for symbol, listed, expired in authoritative.get(exchange, [])
                    if listed <= day <= expired
                )
                absent = sorted(expected - by_day.get((day, exchange), set()))
                if absent:
                    missing.append({"date": day, "exchange": exchange, "symbols": absent})
    gaps = exchange_session_gaps(config, dataset, start=start, end=end)
    owed = [day for day in owed_sessions(config, dataset, end) if day >= start]
    missing_metadata = sorted(observed - known)
    incomplete = bool(gaps or missing or owed or missing_metadata)
    return {
        "dataset": dataset,
        "start": start,
        "end": end,
        "state": "incomplete" if incomplete else "unverified",
        "sessions": len(wanted),
        "observed_contracts": len(observed),
        "exchange_gaps": gaps,
        "missing_contracts": missing,
        "missing_metadata": missing_metadata,
        "owed_dates": owed,
        "unverified_reasons": unverified,
        "coverage": window.group_by("exchange", "product")
        .agg(
            pl.col("trade_date").min().alias("first"),
            pl.col("trade_date").max().alias("last"),
            pl.col("trade_date").n_unique().alias("sessions"),
            pl.col("symbol").n_unique().alias("contracts"),
        )
        .sort("exchange", "product")
        .to_dicts(),
    }
