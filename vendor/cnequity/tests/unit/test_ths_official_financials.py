"""The ths_official statement adapter, with the announce_date policy pinned.

Field names come from a live response captured 2026-09-11; the published
contract documents a different set, so they are asserted here rather than
trusted.
"""

from datetime import date

import polars as pl
import pytest

from cnequity.adapters.ths_official.financials import (
    BALANCE_FIELDS,
    CASHFLOW_FIELDS,
    fetch_statements,
)


class _StubClient:
    """Answers the two statement endpoints from canned items."""

    def __init__(self, balance=None, cashflow=None, fail=False):
        self.balance = balance or []
        self.cashflow = cashflow or []
        self.fail = fail
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if self.fail:
            raise RuntimeError("upstream refused")
        items = self.balance if "balance" in path else self.cashflow
        return {"item": items}


def _period_ms(year: int, month: int) -> int:
    from datetime import datetime, timedelta, timezone

    return int(datetime(year, month, 30, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


def _balance_item(year=2020, month=6):
    return {
        "period_end_ms": _period_ms(year, month),
        # One year later — the trap this adapter must not fall into.
        "report_date_ms": _period_ms(year + 1, month),
        "assets_total": 1000.0,
        "total_debt": 400.0,
        "holder_equity_total": 600.0,
        "cash": 120.0,
        "accounts_receivable": 30.0,
    }


def _cashflow_item(year=2020, month=6):
    return {
        "period_end_ms": _period_ms(year, month),
        "report_date_ms": _period_ms(year + 1, month),
        "act_cash_flow_net": 90.0,
        "invest_cash_flow_net": -20.0,
        "financing_cash_flow_net": -50.0,
        "pay_fixed_assets_etc_cash": 15.0,
        "cash_equivalents_net_addition": 20.0,
    }


def test_rows_carry_the_borrowed_disclosure_date_not_the_upstream_one():
    """`report_date_ms` is the next year's filing date, so it must never be used.

    Writing it into announce_date would push every disclosure date forward by
    about a year and corrupt the PIT column.
    """
    client = _StubClient(balance=[_balance_item()])
    rows, counters = fetch_statements(
        ["600519.SH"],
        date(2016, 1, 1),
        date(2024, 12, 31),
        client=client,
        announce_dates={("600519.SH", "2020Q2"): date(2020, 8, 29)},
        statement_types=("balance",),
    )
    assert rows
    assert {row["announce_date"] for row in rows} == {date(2020, 8, 29)}
    assert counters["skipped_no_announce_date"] == 0


def test_a_period_with_no_borrowable_date_is_skipped_not_invented():
    client = _StubClient(balance=[_balance_item(), _balance_item(2021, 6)])
    rows, counters = fetch_statements(
        ["600519.SH"],
        date(2016, 1, 1),
        date(2024, 12, 31),
        client=client,
        announce_dates={("600519.SH", "2020Q2"): date(2020, 8, 29)},
        statement_types=("balance",),
    )
    assert {row["report_period"] for row in rows} == {"2020Q2"}
    assert counters["skipped_no_announce_date"] == 1
    assert counters["periods"] == 2


def test_field_mapping_matches_the_captured_response():
    client = _StubClient(balance=[_balance_item()], cashflow=[_cashflow_item()])
    rows, _ = fetch_statements(
        ["600519.SH"],
        date(2016, 1, 1),
        date(2024, 12, 31),
        client=client,
        announce_dates={("600519.SH", "2020Q2"): date(2020, 8, 29)},
    )
    by_type: dict[str, dict[str, float]] = {}
    for row in rows:
        by_type.setdefault(row["statement_type"], {})[row["item_code"]] = row["item_value"]

    assert by_type["balance"] == {
        "total_assets": 1000.0,
        "total_liabilities": 400.0,
        "total_equity": 600.0,
        "monetary_funds": 120.0,
        "accounts_receivable": 30.0,
    }
    assert by_type["cashflow"] == {
        "net_cash_operate": 90.0,
        "net_cash_invest": -20.0,
        "net_cash_finance": -50.0,
        "capex": 15.0,
    }


def test_item_codes_stay_inside_what_the_lake_already_defines():
    """New codes would give the backfilled years a wider shape than every other year.

    `end_cash` in particular has no upstream counterpart: the response carries
    `cash_equivalents_net_addition`, which is the net change and not the closing
    balance, so approximating one from the other would be a fabricated series.
    """
    lake_balance = {
        "accounts_receivable",
        "fixed_assets",
        "inventory",
        "monetary_funds",
        "total_assets",
        "total_equity",
        "total_liabilities",
    }
    lake_cashflow = {"capex", "end_cash", "net_cash_finance", "net_cash_invest", "net_cash_operate"}
    assert set(BALANCE_FIELDS) <= lake_balance
    assert set(CASHFLOW_FIELDS) <= lake_cashflow
    assert "end_cash" not in CASHFLOW_FIELDS
    assert "cash_equivalents_net_addition" not in CASHFLOW_FIELDS.values()


def test_null_and_non_finite_values_are_dropped_not_zeroed():
    item = _balance_item()
    item["assets_total"] = None
    item["total_debt"] = float("inf")
    client = _StubClient(balance=[item])
    rows, _ = fetch_statements(
        ["600519.SH"],
        date(2016, 1, 1),
        date(2024, 12, 31),
        client=client,
        announce_dates={("600519.SH", "2020Q2"): date(2020, 8, 29)},
        statement_types=("balance",),
    )
    codes = {row["item_code"] for row in rows}
    assert "total_assets" not in codes
    assert "total_liabilities" not in codes
    assert "total_equity" in codes


def test_one_failing_symbol_does_not_end_the_sweep():
    client = _StubClient(fail=True)
    rows, counters = fetch_statements(
        ["600519.SH", "000001.SZ"],
        date(2016, 1, 1),
        date(2024, 12, 31),
        client=client,
        announce_dates={},
        statement_types=("balance",),
    )
    assert rows == []
    assert counters["failed_symbols"] == 2


def test_a_window_wider_than_the_upstream_cap_is_refused():
    """The service rejects a span over ten years; fail before spending requests."""
    with pytest.raises(ValueError, match="ten-year cap"):
        fetch_statements(
            ["600519.SH"],
            date(2010, 1, 1),
            date(2024, 12, 31),
            client=_StubClient(),
            announce_dates={},
        )


def test_income_maps_net_profit_to_the_parent_holder_figure():
    """The lake's `net_profit` is 归母; the upstream's own is the total.

    Measured across 240 periods: parent_holder agrees 98.3%, total 39.2%. Reading
    the same-named field would manufacture a 61% disagreement out of nothing.
    """
    from cnequity.adapters.ths_official.financials import INCOME_FIELDS

    assert INCOME_FIELDS["net_profit"] == "parent_holder_net_profit"
    assert "net_profit" not in INCOME_FIELDS.values()


def test_income_leaves_unmapped_codes_alone():
    """`finance_expense` is 财务费用; `interest_expenses` is narrower, not a synonym."""
    from cnequity.adapters.ths_official.financials import INCOME_FIELDS

    assert "finance_expense" not in INCOME_FIELDS
    assert "net_profit_deducted" not in INCOME_FIELDS
    assert "interest_expenses" not in INCOME_FIELDS.values()


def test_income_statements_are_fetched_when_asked_for():
    client = _StubClient()
    client.balance = []
    fetch_statements(
        ["600519.SH"],
        date(2016, 1, 1),
        date(2024, 12, 31),
        client=client,
        announce_dates={},
        statement_types=("income",),
    )
    assert [call[0] for call in client.calls] == ["/api/a-share/financials/income-statements"]


def test_the_peer_check_is_silent_without_a_snapshot(tmp_path):
    from cnequity.config import Config
    from cnequity.quality.cross_checks import financial_statement_peer_findings

    assert financial_statement_peer_findings(Config(data_root=tmp_path)) == []


def _valuation_lake(tmp_path, rows):
    from cnequity.domain.schemas import data_version_for, with_provenance

    part = tmp_path / "curated" / "valuation_metrics" / "trade_date=2026-09-04"
    part.mkdir(parents=True)
    frame = pl.DataFrame(
        rows,
        schema={
            "symbol": pl.Utf8,
            "trade_date": pl.Date,
            "pe_ttm": pl.Float64,
            "pb": pl.Float64,
            "ps_ttm": pl.Float64,
            "total_mv": pl.Float64,
            "float_mv": pl.Float64,
            "source": pl.Utf8,
        },
    )
    out = pl.concat(
        [
            with_provenance(
                frame.filter(pl.col("source") == src).drop("source"),
                source=src,
                data_version=data_version_for("valuation_metrics"),
            )
            for src in frame["source"].unique().to_list()
        ],
        how="vertical",
    )
    out.write_parquet(part / "part-merged.parquet")


def test_a_ratio_column_holding_amounts_is_named_with_its_source(tmp_path):
    """96.7% of EastMoney's ps_ttm exceeds 1000 with a median of 3.4e7.

    The adapter reads field f45, which is a profit or revenue figure and not
    市销率. A shape test catches it with no second source at all, which is the
    point — the break was visible in the numbers the whole time.
    """

    from cnequity.config import Config
    from cnequity.quality.unit_checks import valuation_ratio_unit_findings

    _valuation_lake(
        tmp_path,
        {
            # Distinct symbols per source: the primary key is
            # (symbol, trade_date), so one symbol cannot hold two sources on one
            # day. In the lake they never collide either — baostock carries the
            # history and EastMoney the recent sessions.
            "symbol": ["600519.SH", "000001.SZ", "600036.SH", "000002.SZ"],
            "trade_date": [date(2026, 9, 4)] * 4,
            "pe_ttm": [18.7, 4.5, 19.6, 5.2],
            "pb": [6.6, 0.5, 6.3, 0.49],
            # One source reports the ratio; the other reports revenue in yuan.
            "ps_ttm": [4.45e10, 2.57e10, 9.2, 1.7],
            "total_mv": [None] * 4,
            "float_mv": [None] * 4,
            "source": ["eastmoney", "eastmoney", "baostock", "baostock"],
        },
    )
    findings = valuation_ratio_unit_findings(Config(data_root=tmp_path), date(2026, 9, 4))
    assert len(findings) == 1
    assert findings[0]["column"] == "ps_ttm"
    assert findings[0]["source"] == "eastmoney"
    assert findings[0]["severity"] == "error"


def test_plausible_ratios_raise_nothing(tmp_path):
    from cnequity.config import Config
    from cnequity.quality.unit_checks import valuation_ratio_unit_findings

    _valuation_lake(
        tmp_path,
        {
            "symbol": ["600519.SH", "000001.SZ"],
            "trade_date": [date(2026, 9, 4)] * 2,
            "pe_ttm": [18.7, 4.5],
            "pb": [6.6, 0.5],
            "ps_ttm": [9.2, 1.7],
            "total_mv": [None] * 2,
            "float_mv": [None] * 2,
            "source": ["baostock", "baostock"],
        },
    )
    assert valuation_ratio_unit_findings(Config(data_root=tmp_path), date(2026, 9, 4)) == []
