"""EastMoney index membership snapshots; constituent weights are unavailable."""

from __future__ import annotations

import logging
from datetime import date

import polars as pl

from cnequity.adapters.eastmoney.common import exchange_from_datacenter, symbol_from_em
from cnequity.adapters.eastmoney.datacenter import fetch_datacenter
from cnequity.adapters.eastmoney.em_auth import EastMoneyClient
from cnequity.domain.market_time import last_closed_session
from cnequity.domain.symbols import format_symbol

DEFAULT_INDICES = [
    "000001.SH",
    "000300.SH",
    "000688.SH",
    "399001.SZ",
    "399006.SZ",
]

_INDEX_CODE_MAP = {
    "000001": "000001.SH",
    "000300": "000300.SH",
    "000688": "000688.SH",
    "399001": "399001.SZ",
    "399006": "399006.SZ",
}

_REPORT = "RPT_INDEX_CONSTITUENT"
_COLUMNS = "INDEX_CODE,SECURITY_CODE,TRADE_DATE"

logger = logging.getLogger(__name__)


def _index_symbol(index_code: str) -> str:
    code = str(index_code).zfill(6)
    return _INDEX_CODE_MAP.get(code, format_symbol(code, "SH" if code.startswith("0") else "SZ"))


def fetch_index_constituents(
    as_of_date: date,
    *,
    indices: list[str] | None = None,
    client: EastMoneyClient | None = None,
    config=None,
) -> pl.DataFrame:
    if indices == []:
        return pl.DataFrame()
    from cnequity.orchestrator.outcomes import SourceUnavailableError

    if as_of_date != last_closed_session():
        raise SourceUnavailableError(
            "index_constituents: EastMoney serves current membership only; "
            "an older as_of_date would introduce survivorship bias. "
            "Use retained snapshots or supported CNI adjustment history."
        )
    owns = client is None
    if client is None:
        client = EastMoneyClient(config=config)

    # ``None`` means use the default universe; an explicit empty list is a
    # deliberate no-op and must not turn into a full-index snapshot.
    target_indices = DEFAULT_INDICES if indices is None else indices
    rows: list[dict] = []
    missing_indices: list[str] = []
    try:
        for index_sym in target_indices:
            index_code = index_sym.split(".")[0]
            raw = fetch_datacenter(
                client,
                _REPORT,
                _COLUMNS,
                filter_expr=f'(INDEX_CODE="{index_code}")',
                page_size=5000,
            )
            # Effective dates validate current membership; they cannot recover
            # removed members or reconstruct an older snapshot.
            latest: dict[str, tuple[date, dict]] = {}
            for item in raw:
                returned_code = str(item.get("INDEX_CODE") or "").zfill(6)
                if returned_code != index_code.zfill(6):
                    logger.warning(
                        "EastMoney index constituents: requested %s, received %s",
                        index_code,
                        returned_code,
                    )
                    continue
                returned_date = str(item.get("TRADE_DATE") or "")[:10]
                try:
                    effective_date = date.fromisoformat(returned_date)
                except ValueError:
                    logger.warning(
                        "EastMoney index constituents: ignoring invalid member date for %s: %s",
                        index_code,
                        returned_date or "<missing>",
                    )
                    continue
                if effective_date > as_of_date:
                    raise SourceUnavailableError(
                        f"index_constituents: {index_sym} contains a future adjustment "
                        f"dated {returned_date}; current membership cannot recover "
                        f"members removed from the {as_of_date} snapshot"
                    )
                code = str(item.get("SECURITY_CODE", "")).zfill(6)
                previous = latest.get(code)
                if previous is None or effective_date > previous[0]:
                    latest[code] = (effective_date, item)

            matched = 0
            for _effective_date, item in latest.values():
                returned_code = str(item.get("INDEX_CODE") or "").zfill(6)
                code = str(item.get("SECURITY_CODE", "")).zfill(6)
                exch = exchange_from_datacenter(item)
                sym = symbol_from_em(code, 1 if exch == "SH" else (2 if exch == "BJ" else 0))
                if not sym:
                    continue
                matched += 1
                rows.append(
                    {
                        "index_symbol": _index_symbol(returned_code),
                        "symbol": sym,
                        "as_of_date": as_of_date,
                        # EastMoney RPT_INDEX_CONSTITUENT no longer exposes constituent weights.
                        "weight": None,
                    }
                )
            if matched == 0:
                missing_indices.append(index_sym)
    finally:
        if owns:
            client.close()

    if missing_indices:
        raise RuntimeError(
            "EastMoney index constituents returned no matching rows for: "
            + ", ".join(missing_indices)
        )
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema_overrides={"weight": pl.Float64}).unique(
        subset=["index_symbol", "symbol", "as_of_date"], keep="last"
    )
