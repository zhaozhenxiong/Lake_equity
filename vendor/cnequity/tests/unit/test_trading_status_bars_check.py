"""trading_status × daily_bars: a status day must agree with what traded.

A cached fallback once published 4,863 trading names as suspended, and the
day before it wrote no row at all for most of Shenzhen. Nothing in the audit
compared the two datasets, so both days published as clean.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl

from cnequity.config import Config
from cnequity.quality.cross_checks import (
    STATUS_BARS_ERROR_CONFLICTS,
    STATUS_BARS_WARN_CONFLICTS,
    trading_status_bars_findings,
)

D1 = date(2026, 9, 29)
D2 = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def _symbols(n: int) -> list[str]:
    return [f"{1 + i:06d}.SZ" for i in range(n)]


def _lake(tmp_path, status: dict[date, dict[str, str]], traded: dict[date, list[str]]) -> Config:
    root = tmp_path / "data"
    for day, rows in status.items():
        part = root / "curated" / "trading_status" / f"trade_date={day:%Y-%m}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": list(rows),
                "trade_date": [day] * len(rows),
                "is_trading": [value == "normal" for value in rows.values()],
                "status": list(rows.values()),
                "risk_warning": [False] * len(rows),
                "source": ["eastmoney"] * len(rows),
                "data_version": ["v1"] * len(rows),
                "fetched_at": [NOW] * len(rows),
            }
        ).write_parquet(part / f"part-{day.isoformat()}.parquet")
    for day, symbols in traded.items():
        part = root / "curated" / "daily_bars" / f"trade_date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": symbols,
                "trade_date": [day] * len(symbols),
                "close": [10.0] * len(symbols),
                "volume": [1_000] * len(symbols),
                "source": ["tdx_protocol"] * len(symbols),
                "data_version": ["v1"] * len(symbols),
                "fetched_at": [NOW] * len(symbols),
            }
        ).write_parquet(part / "part-0.parquet")
    return Config(data_root=root)


def test_agreeing_days_produce_no_finding(tmp_path):
    symbols = _symbols(80)
    cfg = _lake(
        tmp_path,
        {D1: dict.fromkeys(symbols, "normal"), D2: dict.fromkeys(symbols, "normal")},
        {D1: symbols, D2: symbols},
    )
    assert trading_status_bars_findings(cfg, D2) == []


def test_a_mass_suspension_of_traded_names_is_an_error(tmp_path):
    symbols = _symbols(STATUS_BARS_ERROR_CONFLICTS + 10)
    cfg = _lake(
        tmp_path,
        {D1: dict.fromkeys(symbols, "normal"), D2: dict.fromkeys(symbols, "suspended")},
        {D1: symbols, D2: symbols},
    )

    (finding,) = trading_status_bars_findings(cfg, D2)

    assert finding["check"] == "trading_status_contradicts_bars"
    assert finding["severity"] == "error"
    assert finding["by_date"] == {D2.isoformat(): len(symbols)}


def test_routine_board_disagreement_stays_quiet(tmp_path):
    symbols = _symbols(80)
    status = dict.fromkeys(symbols, "normal")
    status.update(dict.fromkeys(symbols[:STATUS_BARS_WARN_CONFLICTS], "suspended"))
    cfg = _lake(tmp_path, {D2: status}, {D2: symbols})
    assert trading_status_bars_findings(cfg, D2) == []


def test_traded_names_without_a_status_row_are_reported(tmp_path):
    symbols = _symbols(STATUS_BARS_ERROR_CONFLICTS + 10)
    covered = symbols[:5]
    cfg = _lake(
        tmp_path,
        {D1: dict.fromkeys(symbols, "normal"), D2: dict.fromkeys(covered, "normal")},
        {D1: symbols, D2: symbols},
    )

    (finding,) = trading_status_bars_findings(cfg, D2)

    assert finding["check"] == "trading_status_missing_traded"
    assert finding["severity"] == "error"
    assert finding["by_date"] == {D2.isoformat(): len(symbols) - len(covered)}


def test_names_entering_the_feed_are_not_missing_before_their_first_row(tmp_path):
    """Beijing joined the status feed on 2026-09-15; earlier bars are no gap."""
    old = _symbols(20)
    joined = [f"{920000 + i:06d}.BJ" for i in range(STATUS_BARS_WARN_CONFLICTS + 5)]
    cfg = _lake(
        tmp_path,
        {D1: dict.fromkeys(old, "normal"), D2: dict.fromkeys(old + joined, "normal")},
        {D1: old + joined, D2: old + joined},
    )
    assert trading_status_bars_findings(cfg, D2) == []


def test_a_status_day_whose_bars_have_not_landed_is_not_judged(tmp_path):
    symbols = _symbols(80)
    cfg = _lake(
        tmp_path,
        {D1: dict.fromkeys(symbols, "normal"), D2: dict.fromkeys(symbols, "suspended")},
        {D1: symbols},
    )
    assert trading_status_bars_findings(cfg, D2) == []


def test_a_lake_without_bars_has_nothing_to_compare(tmp_path):
    cfg = _lake(tmp_path, {D2: {"000001.SZ": "suspended"}}, {})
    assert trading_status_bars_findings(cfg, D2) == []
