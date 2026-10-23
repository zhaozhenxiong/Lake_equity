from __future__ import annotations

from datetime import date

from cnequity.adapters.cninfo import reviewed_holder_notices as holder


def test_holder_cash_is_not_ex_price_average(monkeypatch):
    key = ("600025.SH", date(2020, 6, 19))
    monkeypatch.setattr(
        holder,
        "_REVIEWED",
        {
            key: {
                "announcement_id": "notice",
                "pdf_sha256": "a" * 64,
                "old_cash": holder.Decimal("0.15"),
                "tradable_a": holder.Decimal("0.18"),
                "other_class": "founder",
                "other_cash": holder.Decimal("0.14913"),
                "payment_date": key[1],
            }
        },
    )
    old = {"symbol": key[0], "ex_date": key[1], "cash_dividend": 0.15}
    text = (
        "证券代码：600025 社会公众股每股实际分派现金红利0.18元；"
        "上市前原三家大股东甲公司每股实际分派现金红利0.14913元。"
        "股份类别股权登记日最后交易日除权（息）日现金红利发放日"
        "Ａ股2020/6/18－2020/6/192020/6/19"
        "对应每股现金红利0.15元。"
    )
    result = holder.reviewed_holder_notice(old, text, "notice", "a" * 64)
    assert result["cash_dividend"] == 0.18
    assert result["record_date"] == date(2020, 6, 18)
    assert result["holder_classes"] == {"tradable_a": "0.18", "founder": "0.14913"}
    assert holder.reviewed_holder_notice(old, text, "wrong", "a" * 64) is None
    assert holder.reviewed_holder_notice(old, text, "notice", "b" * 64) is None
    assert (
        holder.reviewed_holder_notice({**old, "cash_dividend": 0.14}, text, "notice", "a" * 64)
        is None
    )
    assert (
        holder.reviewed_holder_notice(old, text.replace("0.18元", "0.19元"), "notice", "a" * 64)
        is None
    )
    assert (
        holder.reviewed_holder_notice(
            old, text.replace("2020/6/18", "2020/6/19"), "notice", "a" * 64
        )
        is None
    )


def test_holder_evidence_is_immutable(tmp_path):
    entry = {"ex_date": "2020-06-19", "holder_classes": {"tradable_a": "0.18"}}
    path = holder.save_holder_evidence(tmp_path, "run", "600025.SH", [entry])
    assert holder.save_holder_evidence(tmp_path, "run", "600025.SH", [entry]) == path
    assert '"tradable_a":"0.18"' in path.read_text()


def test_unrestricted_holder_does_not_match_restricted_class():
    text = (
        "每股分配比例无限售股股东每股分配现金红利0.3210元（含税）；"
        "限售股股东每股分配现金红利0.2648元（含税）。相关日期"
    )
    assert holder._amounts(text, "600989.SH") == (
        holder.Decimal("0.3210"),
        holder.Decimal("0.2648"),
    )
