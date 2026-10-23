"""Dataset generations selected once for a composed read.

This pins data, not the quality receipts or operator configuration. Long-term
replay uses a research snapshot including coverage evidence and non-secret
read settings. Legacy lakes without revision pointers remain readable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from cnequity.config import Config
from cnequity.domain.datasets import DATASETS
from cnequity.storage.revisions import resolve_committed_root


@dataclass(frozen=True)
class ReadContext:
    roots: Mapping[str, Path]

    @classmethod
    def capture(
        cls,
        config: Config,
        datasets: Iterable[str],
        revisions: Mapping[str, int | str | None],
    ) -> ReadContext:
        roots = {}
        for dataset in sorted(set(datasets)):
            layer = (
                config.derived_root if DATASETS[dataset].layer == "derived" else config.curated_root
            )
            roots[dataset] = resolve_committed_root(
                layer / dataset,
                dataset=dataset,
                meta_root=config.meta_root,
                revision=revisions.get(dataset),
            )
        return cls(MappingProxyType(roots))


def read_root(config: Config, dataset: str, context: ReadContext | None = None) -> Path:
    """Return a concrete root; scanners must use ``committed=False`` on it."""
    if context is not None:
        # A missing dependency is a programming error, never permission to
        # silently fall back to a newer generation halfway through a query.
        return context.roots[dataset]
    layer = config.derived_root if DATASETS[dataset].layer == "derived" else config.curated_root
    return resolve_committed_root(layer / dataset, dataset=dataset, meta_root=config.meta_root)
