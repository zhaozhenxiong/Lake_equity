"""Publish sealed facts independently of completed source attempts."""

from __future__ import annotations

from pathlib import Path

from cnequity.orchestrator.manifest import Manifest


def publication_files(
    manifest: Manifest, run_id: str, dataset: str, staging_root: Path | None = None
) -> list[Path]:
    """Select immutable validated facts; legacy partial staging stays gated."""
    from cnequity.storage.parquet import StagingWriter

    writer = StagingWriter(staging_root or manifest.db_path.parent.parent / "staging")
    batches = [b for b in manifest.get_batches_for_run(run_id) if b["dataset"] == dataset]
    if any(b["status"] in {"running", "stale", "queued"} for b in batches):
        return []
    legacy_safe = manifest.incomplete_batch_counts_by_dataset(run_id).get(dataset, 0) == 0
    return [
        path
        for path in writer.list_run_files(dataset, run_id)
        if writer.is_sealed(path, dataset, run_id) or legacy_safe
    ]


def refresh_batch_liveness(
    manifest: Manifest,
    run_id: str,
    *,
    stale_after_seconds: float,
) -> int:
    """Promote heartbeat-expired running batches to stale before compact gating."""
    return manifest.promote_running_to_stale(run_id, stale_after_seconds=stale_after_seconds)


def datasets_with_incomplete_batches(manifest: Manifest, run_id: str) -> frozenset[str]:
    """Return dataset names that still have non-success batches for *run_id*."""
    counts = manifest.incomplete_batch_counts_by_dataset(run_id)
    return frozenset(ds for ds, n in counts.items() if n > 0)


def compact_allowed(
    manifest: Manifest,
    run_id: str,
    dataset: str,
    *,
    stale_after_seconds: float | None = None,
) -> tuple[bool, int]:
    """Return (allowed, incomplete_batch_count) for compacting *dataset* in *run_id*."""
    if stale_after_seconds is not None:
        refresh_batch_liveness(manifest, run_id, stale_after_seconds=stale_after_seconds)
    incomplete = manifest.incomplete_batch_counts_by_dataset(run_id).get(dataset, 0)
    return incomplete == 0 or bool(publication_files(manifest, run_id, dataset)), incomplete
