"""Narrow issuer-reviewed economic corrections for corporate actions.

Entries require exact old and issuer amounts, event date, announcement id and
original PDF hash. A general mismatch must never become an automatic repair.
"""

from __future__ import annotations

from datetime import date

_REVIEWED = {
    ("688320.SH", date(2024, 6, 14)): {
        "announcement_id": "1220289873",
        "pdf_sha256": "06fa22c5ffc2fee854cb2b88fc827395f6834a15055a1d43857b3293d0b86bc0",
        "old_cash": 0.11038000583648681,
        "issuer_cash": 0.11,
        "payment_date": date(2024, 6, 14),
    }
}


def reviewed_cash_correction(old: dict, facts: dict, announcement_id: str, digest: str) -> bool:
    row = _REVIEWED.get((str(old["symbol"]), old["ex_date"]))
    if row is None:
        return False
    return bool(
        row["announcement_id"] == announcement_id
        and row["pdf_sha256"] == digest
        and facts["code"] == str(old["symbol"]).split(".", 1)[0]
        and facts["ex_date"] == old["ex_date"]
        and facts["payment_date"] == row["payment_date"]
        and abs(float(old["cash_dividend"]) - row["old_cash"]) <= 1e-12
        and abs(float(facts["cash_dividend"]) - row["issuer_cash"]) <= 1e-12
    )
