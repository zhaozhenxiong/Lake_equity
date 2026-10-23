"""Baostock dividend events for an explicitly scoped delisted-stock repair.

``query_dividend_data`` is not a replacement for the normal corporate-action
sources: it is one request per symbol *and year*, and the free endpoint rejects
BJ symbols.  This adapter therefore keeps the source narrow and returns the
same per-share contract as the TDX/EastMoney adapters.  The caller decides
which symbols are safe to repair.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import date

import polars as pl

from cnequity.adapters.baostock._session import (
    capture_wire,
    check_result,
    fetch_per_symbol,
    to_baostock_symbol,
)
from cnequity.domain.http_policy import SourceCoolingDown
from cnequity.domain.rate_limit import source_request
from cnequity.domain.symbols import parse_symbol
from cnequity.storage.raw_archive import RawArchiveError, RawPayloadArchive, begin_capture

logger = logging.getLogger(__name__)

__all__ = ["fetch_corporate_actions_baostock"]


class DividendConflict(ValueError):
    """Deterministic source contradiction; retrying cannot resolve it."""


def _configured_archive(
    config,
    dataset: str,
    *,
    run_id: str | None = None,
    request_scope: str | None = None,
) -> RawPayloadArchive | None:
    if config is None or not hasattr(config, "meta_root"):
        return None
    should_archive = getattr(config, "should_archive_raw", None)
    if callable(should_archive) and not should_archive(dataset):
        return None
    if not bool(getattr(config, "raw_archive_enabled", True)):
        return None
    scope = str(request_scope or f"dataset:{dataset}")
    nonce = begin_capture(config, dataset, run_id, source="baostock", request_scope=scope)
    return RawPayloadArchive(
        config.meta_root,
        enabled=True,
        datasets=[dataset],
        compression=getattr(config, "raw_archive_compression", "gzip"),
        max_payload_bytes=getattr(config, "raw_archive_max_payload_bytes", None),
        capture_owner=config,
        capture_run_id=run_id,
        capture_source="baostock",
        capture_scope=scope,
        capture_nonce=nonce,
    )


def _result_wire(result) -> bytes | None:
    """Read a protocol capture off the result without manufacturing bytes.

    The published SDK never sets any of these: it decodes the response to
    ``str`` before the result is built.  Real captures come from the socket
    recorder; this stays for fakes and for any SDK that starts exposing them.
    """
    for name in ("raw_bytes", "wire_bytes", "response_bytes", "raw_response"):
        value = getattr(result, name, None)
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, bytes):
            return value
    return None


_OUTPUT_SCHEMA = {
    "symbol": pl.Utf8,
    "ex_date": pl.Date,
    "payment_date": pl.Date,
    "payment_source": pl.Utf8,
    "action_type": pl.Utf8,
    "cash_dividend": pl.Float64,
    "bonus_ratio": pl.Float64,
    "transfer_ratio": pl.Float64,
    "allotment_ratio": pl.Float64,
    "allotment_price": pl.Float64,
}

# Stable field order used by Baostock when a fake/test result does not expose
# ``fields``.  Production parsing is name-based because the endpoint has added
# fields over time.
_FIELDS = (
    "code",
    "dividPreNoticeDate",
    "dividAgmPumDate",
    "dividPlanAnnounceDate",
    "dividPlanDate",
    "dividRegistDate",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
    "dividCashPsBeforeTax",
    "dividCashPsAfterTax",
    "dividStocksPs",
    "dividCashStock",
    "dividReserveToStockPs",
)


def _number(value: object) -> float | None:
    if value is None or str(value).strip() in ("", "null", "None", "-"):
        return 0.0
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _field_positions(result) -> dict[str, int]:
    fields = getattr(result, "fields", None) or []
    names = [str(field) for field in fields]
    if not names:
        names = list(_FIELDS)
    return {name: index for index, name in enumerate(names)}


def _value(row: list[str], positions: dict[str, int], name: str) -> str:
    index = positions.get(name)
    if index is None or index >= len(row):
        return ""
    return row[index]


def _parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _action_rows(
    symbol: str,
    row: list[str],
    positions: dict[str, int],
    start: date,
    end: date,
) -> list[dict]:
    expected_code = to_baostock_symbol(symbol)
    reported_code = _value(row, positions, "code").strip().lower()
    if reported_code and reported_code != expected_code:
        logger.warning(
            "baostock corporate_actions: skipping %s row returned as %s",
            symbol,
            reported_code,
        )
        return []

    ex_date = _parse_date(_value(row, positions, "dividOperateDate"))
    if ex_date is None or not (start <= ex_date <= end):
        return []

    values: dict[str, float] = {}
    for name in (
        "dividCashPsBeforeTax",
        "dividStocksPs",
        "dividReserveToStockPs",
    ):
        parsed = _number(_value(row, positions, name))
        if parsed is None:
            logger.warning(
                "baostock corporate_actions: skipping %s %s row with invalid %s",
                symbol,
                ex_date,
                name,
            )
            return []
        values[name] = max(parsed, 0.0)

    # The numeric field rounds to six decimals, while the same raw response's
    # per-ten-share plan often retains the actual seven-decimal per-share cash.
    # Only use an explicit pretax leading plan, corroborated by the numeric field.
    plan = _value(row, positions, "dividCashStock").strip()
    cash_plan = re.match(r"^10(?:(?:转|送)[0-9]+(?:\.[0-9]+)?)*派([0-9]+(?:\.[0-9]+)?)元", plan)
    if cash_plan:
        # The wire may contain an incomplete duplicate before the complete
        # event. An absent amount is unknown, not a contradictory zero. Skip
        # this record without inventing cash from prose; a complete duplicate
        # can supply evidence, otherwise the repair retains a missing event.
        raw_cash = _value(row, positions, "dividCashPsBeforeTax").strip()
        if raw_cash in ("", "null", "None", "-"):
            logger.warning(
                "baostock corporate_actions: skipping incomplete cash record %s %s",
                symbol,
                ex_date,
            )
            return []
        precise_cash = float(cash_plan.group(1)) / 10.0
        if (
            not math.isfinite(precise_cash)
            or abs(precise_cash - values["dividCashPsBeforeTax"]) > 1e-6
        ):
            raise DividendConflict(
                f"Baostock dividend amount conflicts with plan for {symbol} {ex_date}"
            )
        values["dividCashPsBeforeTax"] = precise_cash

    common = {
        "symbol": symbol,
        "ex_date": ex_date,
        "payment_date": None,
        "payment_source": None,
        "cash_dividend": 0.0,
        "bonus_ratio": 0.0,
        "transfer_ratio": 0.0,
        "allotment_ratio": None,
        "allotment_price": None,
    }
    rows: list[dict] = []
    if values["dividCashPsBeforeTax"] > 0:
        rows.append(
            {
                **common,
                "action_type": "cash_dividend",
                "cash_dividend": values["dividCashPsBeforeTax"],
                "payment_date": _parse_date(_value(row, positions, "dividPayDate")),
                "payment_source": "baostock:dividPayDate"
                if _parse_date(_value(row, positions, "dividPayDate"))
                else None,
            }
        )
    if values["dividStocksPs"] > 0:
        rows.append(
            {
                **common,
                "action_type": "bonus",
                "bonus_ratio": values["dividStocksPs"],
            }
        )
    if values["dividReserveToStockPs"] > 0:
        rows.append(
            {
                **common,
                "action_type": "transfer",
                "transfer_ratio": values["dividReserveToStockPs"],
            }
        )
    return rows


def _fetch_one_corporate_actions(
    bs,
    symbol: str,
    start: date,
    end: date,
    *,
    pace: Callable[[], None] | None = None,
    config=None,
    archive: RawPayloadArchive | None = None,
    run_id: str | None = None,
    request_scope: str | None = None,
    years: set[int] | None = None,
    diagnostics: list[dict] | None = None,
    metrics: dict | None = None,
) -> list[dict] | None:
    """Fetch one symbol's dividend plans over the inclusive year window."""
    # Baostock's dividend endpoint currently rejects BJ codes.  Returning a
    # legitimate empty result keeps the explicit SH/SZ repair path resumable;
    # it must not turn a known source limitation into a failed batch.
    if parse_symbol(symbol).exchange not in {"SH", "SZ"}:
        return []

    rows: list[dict] = []
    for year in sorted(years if years is not None else range(start.year, end.year + 1)):
        if not start.year <= year <= end.year:
            raise ValueError("Requested year outside scope")
        # The endpoint is one request per year, unlike k-data's one request
        # per symbol. Pace each year too; otherwise a long-lived symbol with
        # ten annual queries bypasses the free API's cumulative request guard.
        if pace is not None:
            pace()
        capturing = archive is not None and archive.enabled
        try:
            with capture_wire(capturing) as recorder, source_request(config, "baostock"):
                if metrics is not None:
                    metrics["network_requests"] += 1
                result = check_result(
                    bs.query_dividend_data(
                        to_baostock_symbol(symbol),
                        year,
                        yearType="operate",
                    ),
                    config=config,
                )
        except SourceCoolingDown:
            raise
        except Exception as exc:  # noqa: BLE001 - session helper retries it
            logger.warning(
                "baostock corporate_actions query failed for %s/%s: %s", symbol, year, exc
            )
            return None
        if capturing:
            wire = _result_wire(result)
            if wire is None and recorder is not None and recorder.received:
                wire = bytes(recorder.received)
            if not isinstance(wire, bytes):
                raise RawArchiveError(
                    f"Baostock corporate_actions {symbol}/{year}: response has no exact wire bytes"
                )
            archive.archive(
                "corporate_actions",
                wire,
                source="baostock",
                request_params={
                    "symbol": to_baostock_symbol(symbol),
                    "year": year,
                    "year_type": "operate",
                },
                run_id=run_id,
                payload_format="bytes",
                http_metadata={"wire_exact": True, "protocol": "baostock"},
                observation_id=(
                    f"{run_id or 'anonymous'}:dividend:{symbol}:{year}:"
                    f"scope={request_scope or 'scope-unknown'}"
                ),
                request_scope=request_scope,
            )
        if getattr(result, "error_code", "0") != "0":
            return None
        positions = _field_positions(result)
        while result.next():
            row = result.get_row_data()
            if len(row) < len(positions):
                continue
            rows.extend(_parse_repair_row(symbol, row, positions, start, end, diagnostics))
    return rows


def _parse_repair_row(symbol, row, positions, start, end, diagnostics):
    try:
        return _action_rows(symbol, row, positions, start, end)
    except DividendConflict as exc:
        if diagnostics is None:
            raise
        issue = {
            "symbol": symbol,
            "ex_date": _value(row, positions, "dividOperateDate"),
            "reason": "source_amount_plan_conflict",
            "detail": str(exc),
            "cash": _value(row, positions, "dividCashPsBeforeTax"),
            "plan": _value(row, positions, "dividCashStock"),
        }
        if issue not in diagnostics:
            diagnostics.append(issue)
        return []


def fetch_corporate_actions_baostock(
    symbols: list[str],
    start: date,
    end: date,
    *,
    bs=None,
    sleep=time.sleep,
    config=None,
    symbol_windows: Mapping[str, tuple[date, date]] | None = None,
    run_id: str | None = None,
    archive: RawPayloadArchive | None = None,
    request_scope: str | None = None,
    repair_years: Mapping[str, set[int]] | None = None,
    diagnostics: list[dict] | None = None,
    metrics: dict | None = None,
) -> tuple[pl.DataFrame, list[str]]:
    """Return SH/SZ dividend events and failed symbols over ``[start, end]``.

    The adapter intentionally does not expose allotment events: Baostock's
    dividend endpoint has no reliable allotment ratio/price fields.  Callers
    should preserve TDX/EastMoney rows for that event type.
    """

    if archive is None:
        scope = request_scope or f"repair:baostock:{start.isoformat()}:{end.isoformat()}"
        archive = _configured_archive(
            config,
            "corporate_actions",
            run_id=run_id,
            request_scope=scope,
        )
    if archive is not None and archive.enabled and not run_id:
        raise RawArchiveError("Baostock corporate_actions archive requires a non-empty run_id")

    cached_rows = []
    pending = {s: set(y) for s, y in repair_years.items()} if repair_years is not None else None
    if metrics is not None:
        metrics.update(network_requests=0, replayed_requests=0)
    if pending is not None:
        if end >= date(2025, 1, 1) or any(
            y < start.year or y > end.year for years in pending.values() for y in years
        ):
            raise ValueError("Payment replay scope must precede protected 2025 boundary")
        if archive is not None and archive.enabled:
            from cnequity.adapters.baostock.dividend_replay import DividendReplay

            replay = DividendReplay(
                archive, {(to_baostock_symbol(s), y) for s, years in pending.items() for y in years}
            )
            for symbol, years in pending.items():
                for year in sorted(years.copy()):
                    hit = replay.get(
                        to_baostock_symbol(symbol), year, run_id=run_id, scope=request_scope
                    )
                    if hit is None:
                        continue
                    fields, records = hit
                    positions = {k: i for i, k in enumerate(fields)}
                    a, b = (symbol_windows or {}).get(symbol, (start, end))
                    for row in records:
                        cached_rows.extend(
                            _parse_repair_row(symbol, row, positions, a, b, diagnostics)
                        )
                    years.remove(year)
                    if metrics is not None:
                        metrics["replayed_requests"] += 1

    def fetch_one(bs_session, symbol: str, window_start: date, window_end: date):
        if symbol_windows is not None:
            window_start, window_end = symbol_windows.get(symbol, (window_start, window_end))
        if window_start > window_end:
            return []

        def pace() -> None:
            if config is not None:
                if getattr(config, "source_request", None) is not None:
                    return
                config.rate_limit("baostock")
            else:
                sleep(1.0)

        return _fetch_one_corporate_actions(
            bs_session,
            symbol,
            window_start,
            window_end,
            pace=pace,
            config=config,
            archive=archive,
            run_id=run_id,
            request_scope=request_scope,
            years=pending.get(symbol) if pending is not None else None,
            diagnostics=diagnostics,
            metrics=metrics,
        )

    network_symbols = [s for s in symbols if pending is None or pending.get(s)]
    rows, failed = (
        fetch_per_symbol(
            network_symbols,
            start,
            end,
            fetch_one,
            bs=bs,
            sleep=sleep,
            label="baostock corporate_actions",
            config=config,
            request_managed=True,
        )
        if network_symbols
        else ([], [])
    )
    rows = cached_rows + rows
    if diagnostics is not None:
        fingerprints = {}
        for row in rows:
            key = (row["symbol"], row["ex_date"], row["action_type"])
            fingerprint = tuple(
                row[k] for k in ("cash_dividend", "bonus_ratio", "transfer_ratio", "payment_date")
            )
            if key in fingerprints and fingerprints[key] != fingerprint:
                issue = {
                    "symbol": row["symbol"],
                    "ex_date": str(row["ex_date"]),
                    "reason": "source_duplicate_conflict",
                }
                if issue not in diagnostics:
                    diagnostics.append(issue)
            fingerprints[key] = fingerprint
        blocked = {(r["symbol"], r["ex_date"]) for r in diagnostics}
        rows = [r for r in rows if (r["symbol"], str(r["ex_date"])) not in blocked]
    df = pl.DataFrame(rows, schema=_OUTPUT_SCHEMA) if rows else pl.DataFrame(schema=_OUTPUT_SCHEMA)
    if not df.is_empty():
        df = df.unique(subset=["symbol", "ex_date", "action_type"], keep="last").sort(
            ["ex_date", "symbol", "action_type"]
        )
    return df, failed
