"""Evidence fields shared by point-in-time decision-data repairs.

Observation is a collection fact, not a substitute for public availability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EVIDENCE_STATUSES = frozenset(
    {
        "unreviewed",
        "verified_repaired",
        "source_absent",
        "economic_conflict",
        "identity_unresolved",
        "publication_unknown",
        "not_in_protocol",
    }
)


@dataclass(frozen=True)
class DecisionEvidence:
    source_document_id: str | None
    source_sha256: str | None
    source_published_at: datetime | None
    effective_at: datetime | None
    observed_at: datetime | None
    supersedes_document_id: str | None = None
    evidence_status: str = "unreviewed"

    def __post_init__(self) -> None:
        if self.evidence_status not in EVIDENCE_STATUSES:
            raise ValueError(f"invalid evidence_status: {self.evidence_status}")
        for value in (self.source_published_at, self.effective_at, self.observed_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("evidence timestamps must have timezone information")
        if self.source_sha256 is not None and (
            len(self.source_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.source_sha256)
        ):
            raise ValueError("source_sha256 must be lowercase SHA-256 hex")
        if self.evidence_status == "verified_repaired":
            if not self.source_document_id or not self.source_sha256:
                raise ValueError("verified evidence requires a document id and original hash")

    def published_by(self, decision_at: datetime) -> bool:
        """Unknown publication time fails closed, even if observed_at is earlier."""
        if decision_at.tzinfo is None:
            raise ValueError("decision_at must have timezone information")
        return (
            self.evidence_status == "verified_repaired"
            and self.source_published_at is not None
            and self.source_published_at <= decision_at
        )
