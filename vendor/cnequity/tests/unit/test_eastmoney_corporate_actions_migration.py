"""Offline coverage for legacy BJ → 920xxx EastMoney repair."""

from datetime import date

from cnequity.adapters.eastmoney import corporate_actions_migration as migration


def test_migrated_code_preserves_legacy_suffix():
    assert migration._migrated_code("430564.BJ") == "920564"
    assert migration._migrated_code("870726.BJ") == "920926"
    assert migration._migrated_code("873152.BJ") == "920252"
    assert migration._migrated_code("873305.BJ") == "920505"
    assert migration._migrated_code("600519.SH") is None


def test_fetch_migrated_bj_rewrites_current_source_symbol(monkeypatch):
    seen: list[str] = []

    def fake_fetch(client, report, columns, **kwargs):
        seen.append(kwargs["filter_expr"])
        return [
            {
                "SECURITY_CODE": "920839",
                "SECUCODE": "920839.BJ",
                "EX_DIVIDEND_DATE": "2021-05-20 00:00:00",
                "EQUITY_RECORD_DATE": "2021-05-19 00:00:00",
                "PRETAX_BONUS_RMB": 3.0,
                "BONUS_RATIO": None,
                "IT_RATIO": None,
                "BONUS_IT_RATIO": None,
                "IMPL_PLAN_PROFILE": "10派3.00元(含税)",
                "ASSIGN_PROGRESS": "实施分配",
            }
        ]

    monkeypatch.setattr(migration, "fetch_datacenter", fake_fetch)
    frame, failed = migration.fetch_corporate_actions_eastmoney_migrated_bj(
        ["830839.BJ", "600519.SH"],
        date(2001, 1, 1),
        date(2025, 9, 30),
        client=object(),
    )

    assert failed == []
    assert seen == ['(SECURITY_CODE="920839")']
    assert frame.select("symbol", "ex_date", "action_type", "cash_dividend").to_dicts() == [
        {
            "symbol": "830839.BJ",
            "ex_date": date(2021, 5, 20),
            "action_type": "cash_dividend",
            "cash_dividend": 0.3,
        }
    ]
