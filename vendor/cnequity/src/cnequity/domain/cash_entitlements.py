"""Holder-specific pretax cash rights, separate from ex-price adjustments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from cnequity.domain.decision_evidence import DecisionEvidence

HOLDER_CLASSES = frozenset(
    {
        "tradable_a",
        "restricted_a",
        "founder",
        "strategic_holder",
        "repurchase_account",
        "b_share",
        "h_share",
    }
)


@dataclass(frozen=True)
class CashEntitlement:
    symbol: str
    ex_date: date
    record_date: date
    payment_date: date | None
    holder_class: str
    pretax_cash_per_share: Decimal
    evidence: DecisionEvidence

    def __post_init__(self) -> None:
        if self.holder_class not in HOLDER_CLASSES:
            raise ValueError(f"unknown holder class: {self.holder_class}")
        if self.pretax_cash_per_share < 0:
            raise ValueError("negative cash entitlement")
        if self.holder_class == "repurchase_account" and self.pretax_cash_per_share != 0:
            raise ValueError("repurchase account cannot receive cash if marked excluded")
        if self.record_date > self.ex_date:
            raise ValueError("record date cannot follow ex-date")


def tradable_a_cash(
    entitlements: list[CashEntitlement], *, symbol: str, ex_date: date, decision_at: datetime
) -> CashEntitlement:
    """Return one proved A-share right; ambiguous or unpublished rights block."""
    matches = [
        row
        for row in entitlements
        if row.symbol == symbol
        and row.ex_date == ex_date
        and row.holder_class == "tradable_a"
        and row.evidence.published_by(decision_at)
    ]
    if len(matches) != 1 or matches[0].payment_date is None:
        raise ValueError("tradable A-share cash right is not uniquely verified")
    return matches[0]
