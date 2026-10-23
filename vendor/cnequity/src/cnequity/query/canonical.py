"""Compatibility exports for canonical row helpers."""

from __future__ import annotations

from cnequity.domain.canonical import dedupe_by_primary_key, dedupe_lazy_by_primary_key

__all__ = ["dedupe_by_primary_key", "dedupe_lazy_by_primary_key"]
