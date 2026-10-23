"""Balance sheets and cash-flow statements from the 同花顺 official API.

This exists for one measured gap. The lake's ``financial_statement_items``
covers ``income`` and ``indicator`` for every year, but ``balance`` and
``cashflow`` are empty from 2016 through 2024 — 0 to 37 symbols a year against
4,623 to 5,558 for income. ``steps/fundamentals.py`` has been reporting it as
``backfill_missing_statement_types`` the whole time.

Measured 2026-09-11 for 600519.SH over 2016-01-01..2024-12-31: both endpoints
return 36 periods, exactly nine years of quarters with nothing missing. Against
the rows the lake already holds, the comparable fields agreed at 98-100%.

**``report_date_ms`` is not used, deliberately.** It is not the first-disclosure
date: the value on period P is the disclosure date of the report one year later,
where P appears as the prior-year comparative. Writing it into ``announce_date``
would push every disclosure date forward by roughly a year and quietly corrupt
the PIT column.

Instead the disclosure date is **borrowed from the income statement the lake
already holds** for the same ``(symbol, report_period)``. All four statements
come from one filing, so they share one disclosure date — measured across the
lake, 303,769 of 315,264 multi-statement periods (96.35%) already carry a single
date. A period with no borrowable date is skipped rather than given an invented
one.

**Coverage is thinner than 2025 onward**, because the upstream has no field for
some of what the lake stores. ``fixed_assets`` and ``inventory`` have no
counterpart in the balance sheet, and ``end_cash`` has none in the cash-flow
statement — ``cash_equivalents_net_addition`` is the net change, not the closing
balance. Those item codes stay absent for the backfilled years rather than being
approximated.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cnequity.adapters.ths_official.client import ThsOfficialClient

logger = logging.getLogger(__name__)

__all__ = ["BALANCE_FIELDS", "CASHFLOW_FIELDS", "INCOME_FIELDS", "fetch_statements"]

CST = timezone(timedelta(hours=8))
SOURCE = "ths_official"

# The service caps a request window at ten years.
MAX_WINDOW_DAYS = 365 * 10 - 5

# lake item_code -> upstream field. Only codes the lake already defines; adding
# new ones would give the backfilled years a wider shape than every other year.
BALANCE_FIELDS: dict[str, str] = {
    "total_assets": "assets_total",
    "total_liabilities": "total_debt",
    "total_equity": "holder_equity_total",
    "monetary_funds": "cash",
    "accounts_receivable": "accounts_receivable",
}
# The lake's `net_profit` is 归母净利润, so it maps to `parent_holder_net_profit`
# and never to the upstream's own `net_profit`, which includes minority
# interests. Measured across 240 comparable periods: parent_holder 98.3%,
# total 39.2%. `finance_expense` has no counterpart — `interest_expenses` is
# 利息费用, a narrower thing — and `net_profit_deducted` none at all.
INCOME_FIELDS: dict[str, str] = {
    "revenue": "operating_income",
    "operating_cost": "operating_costs",
    "operating_profit": "operating_profit",
    "total_profit": "profit_total",
    "income_tax": "income_tax_expense",
    "net_profit": "parent_holder_net_profit",
    "manage_expense": "manage_fee",
    "sale_expense": "sales_fee",
}
CASHFLOW_FIELDS: dict[str, str] = {
    "net_cash_operate": "act_cash_flow_net",
    "net_cash_invest": "invest_cash_flow_net",
    "net_cash_finance": "financing_cash_flow_net",
    "capex": "pay_fixed_assets_etc_cash",
}

_ENDPOINTS = (
    ("income", "/api/a-share/financials/income-statements", INCOME_FIELDS),
    ("balance", "/api/a-share/financials/balance-sheets", BALANCE_FIELDS),
    ("cashflow", "/api/a-share/financials/cash-flow-statements", CASHFLOW_FIELDS),
)


def _ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=CST).timestamp() * 1000)


def _report_period(period_end_ms: int | None) -> str | None:
    if not period_end_ms:
        return None
    end = datetime.fromtimestamp(period_end_ms / 1000, tz=CST).date()
    return f"{end.year}Q{(end.month - 1) // 3 + 1}"


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def fetch_statements(
    symbols: list[str],
    start: date,
    end: date,
    *,
    client: ThsOfficialClient,
    announce_dates: dict[tuple[str, str], date],
    statement_types: tuple[str, ...] = ("balance", "cashflow"),
    workers: int = 1,
) -> tuple[list[dict], dict[str, int]]:
    """Fetch long-format statement rows for *symbols* over ``[start, end]``.

    ``announce_dates`` maps ``(symbol, report_period)`` to the disclosure date
    the lake already knows, normally taken from its income rows. A period absent
    from that map is skipped: an invented date is worse than a missing row,
    because ``financial_statement_items`` is a PIT dataset and a reader cannot
    tell a guessed date from a real one.

    Returns the rows and a counters dict, so a caller can report how much was
    dropped for want of a date rather than discovering it as silent shrinkage.
    """
    if (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"window {start}..{end} exceeds the upstream ten-year cap; split it before calling"
        )

    rows: list[dict] = []
    counters = {
        "requests": 0,
        "periods": 0,
        "skipped_no_announce_date": 0,
        "empty_symbols": 0,
        "failed_symbols": 0,
    }
    missing_dates: set[tuple[str, str]] = set()
    lock = threading.Lock()

    def one_symbol(symbol: str) -> None:
        for statement_type, path, mapping in _ENDPOINTS:
            if statement_type not in statement_types:
                continue
            with lock:
                counters["requests"] += 1
            try:
                data = client.get(
                    path,
                    thscode=symbol,
                    period="quarterly",
                    start=_ms(start),
                    end=_ms(end),
                )
            except Exception as exc:  # noqa: BLE001 — one symbol must not end the sweep
                with lock:
                    counters["failed_symbols"] += 1
                logger.warning("ths_official %s failed for %s: %s", statement_type, symbol, exc)
                continue
            items = (data or {}).get("item") or []
            if not items:
                with lock:
                    counters["empty_symbols"] += 1
                continue

            local: list[dict] = []
            periods = 0
            skipped = 0
            local_missing: set[tuple[str, str]] = set()
            for item in items:
                period = _report_period(item.get("period_end_ms"))
                if period is None:
                    continue
                periods += 1
                announce_date = announce_dates.get((symbol, period))
                if announce_date is None:
                    skipped += 1
                    local_missing.add((symbol, period))
                    continue
                for item_code, field in mapping.items():
                    value = _finite(item.get(field))
                    if value is None:
                        continue
                    local.append(
                        {
                            "symbol": symbol,
                            "report_period": period,
                            "statement_type": statement_type,
                            "item_code": item_code,
                            "item_value": value,
                            "announce_date": announce_date,
                        }
                    )
            with lock:
                counters["periods"] += periods
                counters["skipped_no_announce_date"] += skipped
                missing_dates.update(local_missing)
                rows.extend(local)

    if workers > 1:
        # Pacing comes from the shared `ths_official` budget in the rate limiter;
        # the pool only decides how many requests are in flight. Measured
        # 2026-09-11: 4 workers sustained 13 req/s for 2,352 requests with no
        # throttling and flat latency.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one_symbol, symbols))
    else:
        for symbol in symbols:
            one_symbol(symbol)

    if missing_dates:
        sample = sorted(missing_dates)[:5]
        logger.info(
            "ths_official financials: %d period(s) had no borrowable announce_date "
            "and were skipped (e.g. %s)",
            len(missing_dates),
            sample,
        )
    return rows, counters
