#!/usr/bin/env python3
"""Add the optional bitemporal columns to legacy PIT Parquet files.

The migration is intentionally additive and conservative:

* ``observed_at`` is copied from the legacy ``fetched_at`` timestamp;
* ``revision_id`` is a deterministic hash of the business fact, value, and
  provenance;
* ``available_at`` and ``source_published_at`` remain null when the old file
  did not record those source-side times.  Guessing them from a backfill's
  report date would turn a reconstructed value into a false strict vintage.

The four columns are nullable and readers also fill them on the fly, so this
script is optional for correctness.  It is useful when downstream jobs need a
stable physical schema or when a lake owner wants to make the compatibility
migration explicit.  Running it repeatedly is safe: files that already carry
the same values are not rewritten.

Usage::

    scripts/migrations/migrate_pit_vintages.py --config configs/cnequity.toml --dry-run
    scripts/migrations/migrate_pit_vintages.py --config configs/cnequity.toml --apply
    scripts/migrations/migrate_pit_vintages.py --dataset financial_statement_items --apply

Dry-run is the default; ``--apply`` is required to edit curated files.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import polars as pl

from cnequity.adapters.cninfo.announcements import replay_announcement_index_range
from cnequity.config import load_config
from cnequity.domain.pit import (
    PIT_DATASET_NAMES,
    normalize_pit_storage_columns,
)
from cnequity.orchestrator.engine import JobEngine
from cnequity.steps.http_common import verify_raw_archive, write_fetched
from cnequity.storage.atomic import write_parquet_atomic
from cnequity.storage.raw_archive import RawPayloadArchive, begin_capture

DEFAULT_CONFIG = ROOT / "configs/cnequity.toml"


def migrate_frame(df: pl.DataFrame, dataset: str) -> tuple[pl.DataFrame, bool]:
    """Return ``(migrated, changed)`` for one PIT frame."""

    if dataset not in PIT_DATASET_NAMES:
        raise ValueError(f"{dataset!r} is not a registered PIT dataset")
    migrated = normalize_pit_storage_columns(df, dataset)
    return migrated, not df.equals(migrated)


def run(curated_root: Path, *, datasets: tuple[str, ...], apply: bool) -> int:
    """Migrate selected PIT datasets below *curated_root*.

    Return a process-style status code.  A missing dataset root is not an
    error: it is common for optional shareholder feeds not to have been
    initialized yet.
    """

    total_files = changed_files = total_rows = 0
    for dataset in datasets:
        root = curated_root / dataset
        files = sorted(root.glob("**/*.parquet")) if root.exists() else []
        if not files:
            print(f"{dataset}: no parquet under {root}")
            continue
        dataset_changed = 0
        for path in files:
            frame = pl.read_parquet(path)
            migrated, changed = migrate_frame(frame, dataset)
            total_files += 1
            total_rows += frame.height
            if not changed:
                continue
            changed_files += 1
            dataset_changed += 1
            if apply:
                write_parquet_atomic(path, migrated, compression="zstd")
        print(
            f"{dataset}: scanned {len(files)} file(s), "
            f"{'rewrote' if apply else 'would rewrite'} "
            f"{dataset_changed} file(s)"
        )

    verb = "Rewrote" if apply else "Would rewrite"
    print(f"\n{verb} {changed_files}/{total_files} PIT file(s), {total_rows:,} row(s) scanned.")
    if not apply:
        print("Dry run — nothing was written. Re-run with --apply to commit.")
    return 0


def _recovered_announcement_rows(existing: pl.DataFrame, replayed: pl.DataFrame) -> pl.DataFrame:
    """Return source-time repairs whose full announcement identity still matches."""

    if existing.is_empty() or replayed.is_empty():
        return existing.head(0)
    identity = ["announcement_id", "symbol", "announce_date", "title", "url"]
    missing = existing.filter(
        pl.col("source_published_at").is_null() | pl.col("available_at").is_null()
    )
    if missing.is_empty():
        return missing
    recovered = missing.drop(["source_published_at", "available_at"]).join(
        replayed.select(identity + ["source_published_at", "available_at"]),
        on=identity,
        how="inner",
    )
    return recovered.filter(
        pl.col("source_published_at").is_not_null() & pl.col("available_at").is_not_null()
    )


def recover_cninfo_announcement_times(
    cfg, *, apply: bool, start: date | None = None, end: date | None = None
) -> int:
    """Replay archived exact CNINFO pages and publish missing source times."""

    dataset = "announcement_index"
    root = cfg.curated_root / dataset
    partitions = sorted(root.glob("announce_date=*/part-merged.parquet"))
    candidates: list[tuple[date, pl.DataFrame]] = []
    for path in partitions:
        frame = pl.read_parquet(path)
        if not {"source_published_at", "available_at"}.issubset(frame.columns):
            continue
        if not frame.select(
            (pl.col("source_published_at").is_null() | pl.col("available_at").is_null()).any()
        ).item():
            continue
        day = date.fromisoformat(path.parent.name.split("=", 1)[1])
        if (start is not None and day < start) or (end is not None and day > end):
            continue
        candidates.append((day, frame))
    if not candidates:
        print("announcement_index: no missing source timestamps")
        return 0

    store = RawPayloadArchive(
        cfg.meta_root,
        enabled=True,
        datasets=[dataset],
        compression=cfg.raw_archive_compression,
        max_payload_bytes=cfg.raw_archive_max_payload_bytes,
    )
    records = [record for record in store.records(dataset) if record.source == "cninfo"]
    repaired: list[tuple[date, pl.DataFrame, list]] = []
    for day, existing in candidates:
        relevant_scopes = set()
        for record in records:
            pagination = record.pagination if isinstance(record.pagination, dict) else {}
            try:
                start = date.fromisoformat(str(pagination.get("slice_start")))
                end = date.fromisoformat(str(pagination.get("slice_end")))
            except ValueError:
                continue
            if start <= day <= end and record.http_metadata.get("wire_exact") is True:
                relevant_scopes.add(record.request_scope)
        day_records = [
            record
            for record in records
            if record.request_scope in relevant_scopes
            and record.http_metadata.get("wire_exact") is True
        ]
        exact_day_records = [
            record
            for record in day_records
            if record.pagination.get("slice_start") == day.isoformat()
            and record.pagination.get("slice_end") == day.isoformat()
        ]
        # A broad request that split archives both its rejected control page
        # and complete one-day children. Prefer the complete child directly;
        # it is a self-contained exact observation and avoids treating the
        # deliberately rejected broad control as publication evidence.
        if exact_day_records:
            day_records = exact_day_records
        if not day_records:
            continue
        replayed = replay_announcement_index_range(
            store,
            day,
            day,
            records=day_records,
        )
        recovered = _recovered_announcement_rows(existing, replayed)
        if not recovered.is_empty():
            repaired.append((day, recovered, day_records))

    rows = sum(frame.height for _, frame, _ in repaired)
    print(
        f"announcement_index: archived source timestamps recoverable for "
        f"{rows:,} row(s) across {len(repaired)} day(s)"
    )
    if not apply or not repaired:
        if not apply:
            print("Dry run — nothing was written. Re-run with --apply to publish.")
        return 0

    engine = JobEngine(cfg)
    run_id = engine.manifest.start_run(
        "migrate_cninfo_announcement_times",
        {"dataset": dataset, "days": [str(day) for day, _, _ in repaired]},
    )
    written = 0
    try:
        for day, frame, day_records in repaired:
            scope = f"archive-replay:announcement:{day.isoformat()}"
            nonce = begin_capture(cfg, dataset, run_id, source="cninfo", request_scope=scope)
            capture = RawPayloadArchive(
                cfg.meta_root,
                enabled=True,
                datasets=[dataset],
                compression=cfg.raw_archive_compression,
                max_payload_bytes=cfg.raw_archive_max_payload_bytes,
                capture_owner=cfg,
                capture_run_id=run_id,
                capture_source="cninfo",
                capture_scope=scope,
                capture_nonce=nonce,
            )
            captured = []
            for index, record in enumerate(day_records):
                captured.append(
                    capture.archive(
                        dataset,
                        store.read(record),
                        source="cninfo",
                        request_params={
                            **record.request_params,
                            "replayed_from": record.metadata_path,
                        },
                        captured_at=datetime.fromisoformat(record.captured_at),
                        run_id=run_id,
                        url=record.url,
                        response_status=record.response_status,
                        payload_format=record.payload_format,
                        http_metadata={"wire_exact": True, "archive_replay": True},
                        pagination=record.pagination,
                        observation_id=f"archive-replay:{day}:{index}:{record.payload_sha256}",
                        request_scope=scope,
                    )
                )
            evidence = verify_raw_archive(
                cfg,
                dataset,
                run_id,
                source="cninfo",
                request_scope=scope,
                records=captured,
            )
            write_fetched(
                cfg,
                run_id,
                dataset,
                frame,
                source="cninfo",
                batch_id=f"archive-replay-{day.isoformat()}",
                raw_archive_evidence=evidence,
            )
            written += frame.height
        compact = engine.run_step("compact", date.today(), run_id)
        status = "success" if compact.get("status") == "success" else "failed"
        engine.manifest.finish_run(
            run_id,
            status,
            rows_read=written,
            rows_written=written,
            error_message=None if status == "success" else "compact failed",
        )
        print(
            json.dumps({"run_id": run_id, "rows_written": written, "compact": compact}, default=str)
        )
        return 0 if status == "success" else 1
    except Exception as exc:
        engine.manifest.finish_run(run_id, "failed", error_message=str(exc))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--dataset",
        action="append",
        choices=sorted(PIT_DATASET_NAMES),
        help="Migrate one PIT dataset (repeatable; default: all PIT datasets).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without it the script only reports.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit no-op form of the default behaviour.",
    )
    parser.add_argument(
        "--recover-cninfo-announcement-times",
        action="store_true",
        help="Replay archived exact CNINFO pages and publish missing source timestamps.",
    )
    parser.add_argument("--start", type=date.fromisoformat, help="Inclusive recovery start date.")
    parser.add_argument("--end", type=date.fromisoformat, help="Inclusive recovery end date.")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    cfg = load_config(args.config)
    if args.recover_cninfo_announcement_times:
        if (args.start is None) != (args.end is None):
            parser.error("--start and --end must be supplied together")
        if args.start is not None and args.start > args.end:
            parser.error("--start must not be after --end")
        return recover_cninfo_announcement_times(
            cfg,
            apply=args.apply,
            start=args.start,
            end=args.end,
        )
    datasets = tuple(args.dataset or sorted(PIT_DATASET_NAMES))
    print(f"PIT bitemporal columns under {cfg.curated_root}")
    return run(cfg.curated_root, datasets=datasets, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
