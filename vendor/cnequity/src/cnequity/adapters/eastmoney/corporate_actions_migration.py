"""EastMoney corporate actions queried through migrated 920xxx BJ codes.

Legacy 43/83/87xxx names were moved to 920xxx codes in the BSE migration. The
old code is still the symbol in the lake (and in the historical bars), while
EastMoney's current report keeps the event history under the 920xxx identity.
This adapter is deliberately explicit and per-symbol; it must not change the
normal full-report daily/backfill path.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import date
from functools import lru_cache
from pathlib import Path

import polars as pl

from cnequity.adapters.eastmoney.corporate_actions import (
    _COLUMNS,
    _parse_rows,
)
from cnequity.adapters.eastmoney.datacenter import fetch_datacenter
from cnequity.adapters.eastmoney.em_auth import EastMoneyClient
from cnequity.config import Config
from cnequity.domain.symbols import parse_symbol
from cnequity.storage.raw_archive import RawArchiveError, RawPayloadArchive, begin_capture

logger = logging.getLogger(__name__)

__all__ = [
    "fetch_corporate_actions_eastmoney_migrated_bj",
    "migrated_bj_request_scope",
]


def _configured_archive(
    config,
    dataset: str,
    *,
    run_id: str | None = None,
    request_scope: str | None = None,
    source: str = "eastmoney",
) -> RawPayloadArchive | None:
    if config is None or not hasattr(config, "meta_root"):
        return None
    should_archive = getattr(config, "should_archive_raw", None)
    if callable(should_archive) and not should_archive(dataset):
        return None
    if not bool(getattr(config, "raw_archive_enabled", True)):
        return None
    scope = str(request_scope or f"dataset:{dataset}")
    nonce = begin_capture(config, dataset, run_id, source=source, request_scope=scope)
    return RawPayloadArchive(
        config.meta_root,
        enabled=True,
        datasets=[dataset],
        compression=getattr(config, "raw_archive_compression", "gzip"),
        max_payload_bytes=getattr(config, "raw_archive_max_payload_bytes", None),
        capture_owner=config,
        capture_run_id=run_id,
        capture_source=source,
        capture_scope=scope,
        capture_nonce=nonce,
    )


_OUTPUT_SCHEMA = {
    "symbol": pl.Utf8,
    "ex_date": pl.Date,
    "action_type": pl.Utf8,
    "cash_dividend": pl.Float64,
    "bonus_ratio": pl.Float64,
    "transfer_ratio": pl.Float64,
    "allotment_ratio": pl.Float64,
    "allotment_price": pl.Float64,
}

# Snapshot of BSE's published old/new code table (bse.cn/service/code_mapping.html).
# It is data, not a suffix heuristic: collisions such as 870726→920926 and
# 873305→920505 are real and would otherwise query another company's history.
_MAPPING_PATH = Path(__file__).with_name("seeds") / "bse_code_mapping.json"


@lru_cache(maxsize=1)
def _code_mapping() -> dict[str, str]:
    try:
        raw = json.loads(_MAPPING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"BSE legacy/current code mapping is unreadable: {_MAPPING_PATH}"
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"BSE legacy/current code mapping is not an object: {_MAPPING_PATH}")
    return {str(old): str(new) for old, new in raw.items()}


def _migrated_code(symbol: str) -> str | None:
    """Map one legacy BJ code to the current 920xxx code, if applicable."""
    try:
        info = parse_symbol(symbol)
    except ValueError:
        return None
    if info.exchange != "BJ" or len(info.code) != 6 or not info.code.isdigit():
        return None
    return _code_mapping().get(info.code)


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=_OUTPUT_SCHEMA)


def migrated_bj_request_scope(symbols: list[str], start: date, end: date) -> str:
    """Return the stable scope for one migrated-code repair request."""
    symbols_scope = ",".join(sorted({str(symbol) for symbol in symbols}))
    symbols_scope = hashlib.sha256(symbols_scope.encode("utf-8")).hexdigest()[:16]
    return f"repair:eastmoney_bj:{start.isoformat()}:{end.isoformat()}:{symbols_scope}"


def fetch_corporate_actions_eastmoney_migrated_bj(
    symbols: list[str],
    start: date,
    end: date,
    *,
    config: Config | None = None,
    symbol_windows: Mapping[str, tuple[date, date]] | None = None,
    client: EastMoneyClient | None = None,
    run_id: str | None = None,
    archive: RawPayloadArchive | None = None,
    request_scope: str | None = None,
) -> tuple[pl.DataFrame, list[str]]:
    """Fetch actions for legacy BJ symbols through current 920xxx identities."""
    owns_client = client is None
    if client is None:
        client = EastMoneyClient(config=config)
    if archive is None:
        if request_scope is None:
            request_scope = migrated_bj_request_scope(symbols, start, end)
        archive = _configured_archive(
            config,
            "corporate_actions",
            run_id=run_id,
            request_scope=request_scope,
            source="eastmoney_migrated_bj",
        )
    if archive is not None and archive.enabled and not run_id:
        raise RawArchiveError(
            "EastMoney migrated corporate_actions archive requires a non-empty run_id"
        )

    rows: list[dict] = []
    failed: list[str] = []
    seen: set[str] = set()
    try:
        for symbol in symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            current_code = _migrated_code(symbol)
            if current_code is None:
                continue
            window = symbol_windows.get(symbol, (start, end)) if symbol_windows else (start, end)
            if window[0] > window[1]:
                continue
            try:
                datacenter_kwargs = {
                    "filter_expr": f'(SECURITY_CODE="{current_code}")',
                    "page_size": 500,
                    "sort_columns": "EX_DIVIDEND_DATE",
                    "sort_types": "1",
                    "max_retries": config.max_retries if config is not None else 3,
                    "retry_backoff_seconds": (
                        float(config.retry_backoff_seconds) if config is not None else 5.0
                    ),
                }
                if archive is not None:
                    datacenter_kwargs.update(
                        {
                            "archive": archive,
                            "archive_dataset": "corporate_actions",
                            "archive_run_id": run_id,
                            "archive_source": "eastmoney_migrated_bj",
                        }
                    )
                raw = fetch_datacenter(client, "RPT_SHAREBONUS_DET", _COLUMNS, **datacenter_kwargs)
                for item in raw:
                    for parsed in _parse_rows(item):
                        if not (window[0] <= parsed["ex_date"] <= window[1]):
                            continue
                        parsed["symbol"] = symbol
                        rows.append(parsed)
            except Exception as exc:  # noqa: BLE001 — preserve other symbols for retry
                if isinstance(exc, RawArchiveError):
                    raise
                failed.append(symbol)
                logger.warning(
                    "eastmoney migrated corporate_actions: failed for %s/%s: %s",
                    symbol,
                    current_code,
                    exc,
                )
    finally:
        if owns_client:
            client.close()

    if not rows:
        return _empty(), failed
    frame = pl.DataFrame(rows, schema_overrides={"allotment_price": pl.Float64})
    return (
        frame.unique(subset=["symbol", "ex_date", "action_type"], keep="last").sort(
            ["ex_date", "symbol", "action_type"]
        ),
        failed,
    )
