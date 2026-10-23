"""Coverage shortfalls do not invalidate independently validated source rows."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

import polars as pl

from cnequity.orchestrator.outcomes import SourceUnavailableError

_CURRENT: ContextVar[tuple | None] = ContextVar("source_gap_scope", default=None)


@contextmanager
def source_gap_scope(config):
    findings: list[dict] = []
    token = _CURRENT.set((config, findings))
    try:
        yield findings
    finally:
        _CURRENT.reset(token)


def source_gap_dates(dataset: str) -> set[date]:
    current = _CURRENT.get()
    if current is None:
        return set()
    return {
        date.fromisoformat(day)
        for finding in current[1]
        if finding["dataset"] == dataset
        for day in finding["dates"]
    }


def record_source_gap(dataset: str, message: str, *, frame=None, dates=()) -> None:
    """Strict callers can reject coverage; command callers retain valid rows.

    This only relaxes coverage counts. Schema, scope and value validation stay
    at the normal writer boundary, including quarantine of malformed rows.
    """
    current = _CURRENT.get()
    if current is None:
        raise SourceUnavailableError(message)
    from cnequity.domain.datasets import DATASETS
    from cnequity.storage.state import StateStore

    config, findings = current
    days = set(dates)
    spec = DATASETS.get(dataset)
    column = spec.partition_col if spec is not None else None
    if frame is not None and column in frame.columns:
        days.update(frame[column].cast(pl.Date, strict=False).drop_nulls().to_list())
    findings.append(
        {
            "dataset": dataset,
            "severity": "warning",
            "check": "source_scope_incomplete",
            "message": message,
            "dates": sorted(day.isoformat() for day in days),
        }
    )
    if days:
        StateStore(config.meta_root).record_missing_dates(
            dataset, days, reason="source_scope_incomplete"
        )
