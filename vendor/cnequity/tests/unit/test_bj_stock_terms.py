"""Issuer-backed duplicate stock-distribution correction."""

import hashlib
from datetime import date

import pytest

from cnequity.adapters.eastmoney import bse_stock_terms


def test_reviewed_correction_neutralises_only_spurious_bonus(monkeypatch):
    payload = b"%PDF synthetic"
    text = (
        "北京证券交易所证券代码：831305向全体股东每10股转增10股，"
        "其中以其他资本公积每10股转增0股，每10股派9元人民币现金。"
    )
    monkeypatch.setattr(
        bse_stock_terms,
        "PdfReader",
        lambda stream: type(
            "Reader", (), {"pages": [type("Page", (), {"extract_text": lambda self: text})()]}
        )(),
    )
    rows = [
        {
            "symbol": "920405.BJ",
            "action_type": "bonus",
            "bonus_ratio": 1.0,
            "transfer_ratio": 0.0,
            "source": "tdx_protocol",
        },
        {
            "symbol": "920405.BJ",
            "action_type": "transfer",
            "bonus_ratio": 0.0,
            "transfer_ratio": 1.0,
            "source": "ths",
        },
        {
            "symbol": "920405.BJ",
            "action_type": "cash_dividend",
            "bonus_ratio": 0.0,
            "transfer_ratio": 0.0,
            "source": "eastmoney",
        },
    ]
    expected = ("notice", hashlib.sha256(payload).hexdigest(), 1.0)
    corrected = bse_stock_terms.verified_bonus_correction(rows, payload, expected)
    assert corrected["bonus_ratio"] == 0.0
    assert corrected["source"] == "eastmoney"
    with pytest.raises(ValueError, match="PDF changed"):
        bse_stock_terms.verified_bonus_correction(rows, payload + b"x", expected)
    rows[1]["transfer_ratio"] = 0.5
    with pytest.raises(ValueError, match="stored stock-terms amount"):
        bse_stock_terms.verified_bonus_correction(rows, payload, expected)


def test_corrected_notice_changes_classification_and_payment_only(monkeypatch):
    original = bse_stock_terms.CORRECTED_CHAIN
    correction, final = b"%PDF correction", b"%PDF final"
    monkeypatch.setattr(
        bse_stock_terms,
        "CORRECTED_CHAIN",
        {
            **original,
            "correction": ("correction", hashlib.sha256(correction).hexdigest()),
            "final": ("final", hashlib.sha256(final).hexdigest()),
        },
    )
    texts = {
        correction: "证券代码：830799权益分派更正除权除息参考价",
        final: (
            "证券代码：830799每10股转增5股每10股派人民币现金2.5元"
            "权益登记日为：2022年5月31日除权除息日为：2022年6月1日"
            "现金红利将于2022年6月1日"
        ),
    }
    monkeypatch.setattr(
        bse_stock_terms,
        "PdfReader",
        lambda stream: type(
            "Reader",
            (),
            {
                "pages": [
                    type("Page", (), {"extract_text": lambda self: texts[stream.getvalue()]})()
                ]
            },
        )(),
    )
    rows = [
        {
            "symbol": "920799.BJ",
            "ex_date": date(2022, 6, 1),
            "action_type": "bonus",
            "bonus_ratio": 0.5,
            "transfer_ratio": 0.0,
            "cash_dividend": 0.0,
        },
        {
            "symbol": "920799.BJ",
            "ex_date": date(2022, 6, 1),
            "action_type": "cash_dividend",
            "bonus_ratio": 0.0,
            "transfer_ratio": 0.0,
            "cash_dividend": 0.25,
            "payment_date": None,
        },
    ]
    result = bse_stock_terms.verified_correction_chain(rows, correction, final)
    assert [(r["action_type"], r["bonus_ratio"], r["transfer_ratio"]) for r in result] == [
        ("bonus", 0.0, 0.0),
        ("transfer", 0.0, 0.5),
        ("cash_dividend", 0.0, 0.0),
    ]
    assert result[-1]["payment_date"] == date(2022, 6, 1)
    with pytest.raises(ValueError, match="final economic"):
        texts[final] = texts[final].replace("每10股转增5股", "每10股送5股")
        bse_stock_terms.verified_correction_chain(rows, correction, final)
