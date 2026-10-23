from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import uuid4

import polars as pl

from cnequity.domain.action_evidence import clear_invalid_payment_evidence
from cnequity.domain.canonical import dedupe_by_primary_key
from cnequity.domain.datasets import granularity_for_dataset
from cnequity.domain.partitions import Granularity
from cnequity.domain.pit import (
    PIT_DATASET_NAMES,
    PIT_STORAGE_COLUMNS,
    normalize_pit_storage_columns,
    retain_pit_vintages,
)
from cnequity.domain.schemas import (
    DATASET_SCHEMAS,
    OPTIONAL_COLUMNS,
    PRIMARY_KEYS,
    SchemaValidationError,
    sanitize_dataset_rows,
    validate_dataframe,
)
from cnequity.storage.atomic import write_json_atomic, write_parquet_atomic
from cnequity.storage.changes import summarize_changes
from cnequity.storage.revisions import sha256_file

logger = logging.getLogger(__name__)


def _guard_staged_status(curated_root: Path, rows: pl.DataFrame) -> pl.DataFrame:
    """Keep an old successful staging batch from resurrecting invalid statuses."""
    from cnequity.domain.trading_status import (
        DELISTED_SOURCE,
        impossible_delistings,
        split_before_listing,
    )
    from cnequity.query.parquet_scan import dataset_has_parquet, scan_parquet_root

    instruments = curated_root / "instruments"
    if rows.is_empty() or not dataset_has_parquet(instruments):
        return rows
    spans = scan_parquet_root(instruments, hive=False).collect()
    for column in ("list_date", "delist_date"):
        if column not in spans.columns:
            spans = spans.with_columns(pl.lit(None, dtype=pl.Date).alias(column))
    spans = dedupe_by_primary_key(spans, "instruments").select("symbol", "list_date", "delist_date")
    impossible = impossible_delistings(spans)
    flagged = rows.join(spans.select("symbol", "delist_date"), on="symbol", how="left")
    invalid = (pl.col("source") == DELISTED_SOURCE) & (
        pl.col("delist_date").is_null()
        | (pl.col("trade_date") < pl.col("delist_date"))
        | pl.col("symbol").is_in(sorted(impossible))
    )
    kept = flagged.filter(~invalid).drop("delist_date")
    candidates = kept.join(spans.select("symbol", "list_date"), on="symbol").filter(
        pl.col("trade_date") < pl.col("list_date")
    )
    late = kept.join(spans.select("symbol", "delist_date"), on="symbol").filter(
        pl.col("is_trading") & (pl.col("trade_date") > pl.col("delist_date"))
    )
    if not candidates.is_empty() or not late.is_empty():
        affected = sorted(set(candidates["symbol"]) | set(late["symbol"]))
        bars = curated_root / "daily_bars"
        if dataset_has_parquet(bars):
            first = (
                scan_parquet_root(
                    bars,
                    partition_col="trade_date",
                    traded_only=True,
                    symbols=affected,
                )
                .group_by("symbol")
                .agg(
                    pl.col("trade_date").min().alias("first_traded"),
                    pl.col("trade_date").max().alias("last_traded"),
                )
                .collect()
            )
            spans = spans.join(first, on="symbol", how="left")
        else:
            spans = spans.with_columns(
                pl.lit(None, dtype=pl.Date).alias("first_traded"),
                pl.lit(None, dtype=pl.Date).alias("last_traded"),
            )
        kept, _ = split_before_listing(kept, spans)
        retired = spans.filter(
            pl.col("delist_date").is_not_null()
            & ~pl.col("symbol").is_in(sorted(impossible))
            & (pl.col("last_traded").is_null() | (pl.col("last_traded") <= pl.col("delist_date")))
        ).select("symbol", pl.col("delist_date").alias("_delist"))
        kept = (
            kept.join(retired, on="symbol", how="left")
            .filter(
                ~(
                    pl.col("is_trading").fill_null(False)
                    & pl.col("_delist").is_not_null()
                    & (pl.col("trade_date") > pl.col("_delist"))
                )
            )
            .drop("_delist")
        )
    if kept.height < rows.height:
        logger.warning(
            "trading_status: dropped %d staged row(s) outside the listed life",
            rows.height - kept.height,
        )
    return kept


def _dedupe_storage(frame: pl.DataFrame, dataset: str) -> pl.DataFrame:
    if dataset in PIT_DATASET_NAMES:
        return retain_pit_vintages(frame, dataset)
    return dedupe_by_primary_key(frame, dataset)


def _business_equal(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    """Compare business rows exactly, ignoring only observation-time churn.

    Reconciliation re-fetches the tail; fetched_at/observed_at alone must not
    mint a revision. Source, types, multiplicity and source-side PIT timestamps
    still matter. Sort inside Polars rather than serializing every row into
    Python dictionaries and JSON strings. This comparison is local to one
    compact operation, so no persisted digest or revision contract changes.
    """
    ignored = {"fetched_at", "observed_at"}
    columns = sorted(set(left.columns) - ignored)
    if left.height != right.height or columns != sorted(set(right.columns) - ignored):
        return False
    if any(left.schema[column] != right.schema[column] for column in columns):
        return False
    if not columns:
        return True
    return left.select(columns).sort(columns).equals(right.select(columns).sort(columns))


class StagingWriter:
    def __init__(self, staging_root: Path | str):
        # Process-pool workers may pass a str path across the boundary.
        self.staging_root = Path(staging_root)

    def write_batch(
        self,
        dataset: str,
        run_id: str,
        batch_id: str,
        df: pl.DataFrame,
    ) -> Path:
        df = sanitize_dataset_rows(validate_dataframe(df, dataset), dataset)
        out_dir = self.staging_root / dataset / f"run_id={run_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"part-{batch_id}.parquet"
        # A crash during replacement must leave an unsealed file, never the
        # previous attempt's seal attached to different bytes.
        path.with_suffix(".sealed.json").unlink(missing_ok=True)
        write_parquet_atomic(path, df, compression="zstd")
        self._write_seal(path, dataset, run_id, batch_id=batch_id, rows=df.height)
        return path

    def seal_file(
        self, path: Path, dataset: str, run_id: str, *, batch_id: str | None = None
    ) -> None:
        """Seal only readable schema-valid bytes, including filtered candidates."""
        frame = validate_dataframe(pl.read_parquet(path), dataset)
        self._write_seal(path, dataset, run_id, batch_id=batch_id, rows=frame.height)

    @staticmethod
    def _write_seal(
        path: Path, dataset: str, run_id: str, *, batch_id: str | None, rows: int
    ) -> None:
        write_json_atomic(
            path.with_suffix(".sealed.json"),
            {
                "result_schema_version": 2,
                "dataset": dataset,
                "run_id": run_id,
                "batch_id": batch_id or path.stem.removeprefix("part-"),
                "sha256": sha256_file(path),
                "rows": rows,
            },
        )

    def is_sealed(self, path: Path, dataset: str, run_id: str) -> bool:
        """A validation seal names the exact atomically written bytes."""
        receipt = path.with_suffix(".sealed.json")
        if not receipt.exists():
            return False
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        if (
            payload.get("result_schema_version") != 2
            or payload.get("dataset") != dataset
            or payload.get("run_id") != run_id
            or payload.get("sha256") != sha256_file(path)
        ):
            raise ValueError(f"staging seal does not match validated bytes: {path}")
        return True

    def write_usable_batch(
        self,
        dataset: str,
        run_id: str,
        batch_id: str,
        df: pl.DataFrame,
    ) -> dict:
        """Stage the valid rows of a batch whose rows are independent facts.

        Invalid rows are isolated by bisection, so one bad row costs a few
        extra validations rather than the batch. The full input and the rejects
        are kept under ``_quarantine`` as evidence. Accepting some rows never
        certifies coverage: the caller records the rejected dates as missing.
        Only datasets registered with ``partial_rows`` may use this path.
        """
        from cnequity.domain.datasets import DATASETS

        spec = DATASETS.get(dataset)
        if spec is None or not spec.partial_rows:
            raise ValueError(f"{dataset} does not accept partial batches")
        accepted: list[pl.DataFrame] = []
        rejected: list[pl.DataFrame] = []
        reasons: set[str] = set()
        required = DATASET_SCHEMAS.get(dataset, {})

        def split(frame: pl.DataFrame) -> None:
            try:
                accepted.append(validate_dataframe(frame, dataset))
            except SchemaValidationError as exc:
                # A missing column is a batch-level fault; bisecting it would
                # only reject every row one at a time.
                if frame.height <= 1 or any(
                    column not in frame.columns and column not in OPTIONAL_COLUMNS
                    for column in required
                ):
                    rejected.append(frame)
                    reasons.add(str(exc))
                else:
                    middle = frame.height // 2
                    split(frame.head(middle))
                    split(frame.slice(middle))

        split(df)
        count = sum(frame.height for frame in rejected)
        report: dict = {
            "dataset": dataset,
            "run_id": run_id,
            "batch_id": batch_id,
            "rows_read": df.height,
            "rows_rejected": count,
        }
        if rejected:
            safe_run = "".join(c if c.isalnum() or c in "-_" else "_" for c in run_id)
            quarantine = (
                self.staging_root.parent
                / "_quarantine"
                / f"{dataset}-{safe_run}-rows-{uuid4().hex}"
            )
            quarantine.mkdir(parents=True)
            bad = pl.concat(rejected, how="diagonal_relaxed")
            write_parquet_atomic(quarantine / "input.parquet", df, compression="zstd")
            write_parquet_atomic(quarantine / "rejected.parquet", bad, compression="zstd")
            date_col = spec.date_col or spec.partition_col
            days: set[str] = set()
            if date_col and date_col in bad.columns:
                values = (
                    bad.select(
                        pl.col(date_col).cast(pl.String).str.slice(0, 10).str.to_date(strict=False)
                    )
                    .to_series()
                    .drop_nulls()
                )
                days.update(value.isoformat() for value in values)
            report.update(
                quarantine=str(quarantine),
                reasons=sorted(reasons),
                rejected_dates=sorted(days),
            )
            write_json_atomic(quarantine / "report.json", report, indent=2)
        if not accepted:
            raise SchemaValidationError(
                f"{dataset}: all {count} rows rejected; retained at {report.get('quarantine')}"
            )
        good = pl.concat(accepted, how="diagonal_relaxed")
        path = self.write_batch(dataset, run_id, batch_id, good)
        report.update(rows_written=good.height, path=str(path))
        if rejected:
            receipt = self.quality_receipt(dataset, run_id, batch_id)
            write_json_atomic(receipt, report, indent=2)
        return report

    def quality_receipt(self, dataset: str, run_id: str, batch_id: str) -> Path:
        """Where a partially accepted batch records what it left out.

        Inside the run directory, so staging cleanup removes it with the run.
        """
        return self.staging_root / dataset / f"run_id={run_id}" / "_quality" / f"{batch_id}.json"

    def quality_receipts(self, dataset: str, run_id: str) -> list[dict]:
        """Every partial-batch receipt of one run, oldest batch id first."""
        root = self.staging_root / dataset / f"run_id={run_id}" / "_quality"
        return [
            json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))
        ]

    def list_run_files(self, dataset: str, run_id: str) -> list[Path]:
        run_dir = self.staging_root / dataset / f"run_id={run_id}"
        if not run_dir.exists():
            return []
        return sorted(run_dir.rglob("*.parquet"))


class CuratedWriter:
    def __init__(self, curated_root: Path):
        self.curated_root = curated_root

    def partition_path(self, dataset: str, partition_col: str, partition_value: str) -> Path:
        return self.curated_root / dataset / f"{partition_col}={partition_value}"

    def write_partition(
        self,
        dataset: str,
        partition_col: str,
        partition_value: str,
        df: pl.DataFrame,
        part_name: str = "part-0.parquet",
    ) -> Path:
        out_dir = self.partition_path(dataset, partition_col, partition_value)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / part_name
        write_parquet_atomic(path, df, compression="zstd")
        # A previous run may have left fragment files (for example after a
        # staging write used ``part-<batch>.parquet``), possibly under a
        # temporary subdirectory. Keeping them beside the canonical file makes
        # every later recursive scan read the same rows twice, even though the
        # merge above already deduplicated them. Remove only old parquet
        # descendants, and only after the atomic replacement has succeeded so
        # a failed write leaves the old partition readable.
        for stale in out_dir.rglob("*.parquet"):
            if stale != path:
                stale.unlink()
        return path


def _pit_columns_absent(path: Path, dataset: str) -> bool:
    """True when *path* is a PIT dataset file that predates the PIT columns.

    Reads the parquet footer only — the whole point is to avoid the row-wise
    derivation that a file without these columns forces on every reader.
    """
    if dataset not in PIT_DATASET_NAMES or not path.is_file():
        return False
    try:
        stored = set(pl.read_parquet_schema(path))
    except Exception:
        return False
    return not set(PIT_STORAGE_COLUMNS).issubset(stored)


def _partition_values(df: pl.DataFrame, partition_col: str, granularity: Granularity) -> pl.Series:
    """Directory value per row for *partition_col*.

    Date columns map through the dataset's period (day/month/year); non-date
    keys like ``report_period`` ("2024Q1") are already period labels and are
    used verbatim.
    """
    col = df.get_column(partition_col)
    if col.dtype != pl.Date:
        return col.cast(pl.Utf8)
    if granularity == "year":
        return col.dt.strftime("%Y")
    if granularity == "month":
        return col.dt.strftime("%Y-%m")
    return col.dt.strftime("%Y-%m-%d")


def compact_dataset(
    staging_root: Path,
    curated_root: Path,
    dataset: str,
    run_id: str,
    partition_col: str | None = "trade_date",
    granularity: Granularity | None = None,
    changed_files: list[Path] | None = None,
    base_root: Path | None = None,
    change_log: list[dict] | None = None,
    staging_files: list[Path] | None = None,
) -> int:
    """Merge staging batches into curated partitions, dedupe by PK.

    Granularity decides how many dates share one partition directory; it comes
    from the dataset's registry entry unless overridden. See
    ``domain/partitions.py`` for why it is not always a day.
    """
    if granularity is None:
        granularity = granularity_for_dataset(dataset)
    staging = StagingWriter(staging_root)
    curated = CuratedWriter(curated_root)
    # ``base_root`` is the immutable committed generation selected before the
    # write.  It is optional for backwards compatibility with direct callers;
    # finalize passes it so a retry after a crash cannot merge from a half-
    # rewritten mutable compatibility directory.
    read_root = Path(base_root) if base_root is not None else curated_root / dataset
    files = staging.list_run_files(dataset, run_id) if staging_files is None else staging_files
    if not files:
        return 0

    # Re-validate on read: staging/curated written before a schema change
    # (e.g. fetched_at str → timestamp) must be normalized before concat.
    combined = pl.concat(
        [
            sanitize_dataset_rows(validate_dataframe(pl.read_parquet(f), dataset), dataset)
            for f in files
        ],
        how="diagonal_relaxed",
    )
    if dataset == "trading_status":
        combined = _guard_staged_status(curated_root, combined)
        if combined.is_empty():
            return 0
    if dataset == "valuation_metrics":
        from cnequity.domain.valuation import null_missing_valuations

        combined = null_missing_valuations(combined)
    if dataset == "index_constituents":
        from cnequity.domain.membership import null_missing_index_weights

        combined = null_missing_index_weights(combined)
    if dataset == "corporate_actions":
        # A payment before the ex-date is a source typo, not a fact, so a new
        # row never stores one.  Only staged rows are cleaned: rewriting the
        # committed side here would hide the change from the diff below and a
        # stored typo would never be republished without it.
        combined = clear_invalid_payment_evidence(combined)
    pk = PRIMARY_KEYS.get(dataset, [])
    if pk:
        combined = _dedupe_storage(combined, dataset)

    def _pit(frame: pl.DataFrame) -> pl.DataFrame:
        """Materialize the bitemporal columns so they reach disk.

        They were previously derived on every read instead (19 s of row-wise
        hashing for one `financial_statement_items` scan). Applied to both
        sides of the digest comparison below so adding them is never mistaken
        for a business change.
        """
        return normalize_pit_storage_columns(frame, dataset)

    combined = _pit(combined)

    if partition_col not in combined.columns:
        out_dir = curated.curated_root / dataset
        out_path = out_dir / "part-merged.parquet"
        existing_dir = read_root
        existing_files = sorted(existing_dir.rglob("*.parquet")) if existing_dir.exists() else []
        before_digest = sha256_file(out_path) if out_path.is_file() else None
        had_fragments = any(
            path.relative_to(existing_dir) != Path(out_path.name) for path in existing_files
        )
        existing = pl.DataFrame(schema=combined.schema)
        if existing_files:
            existing = pl.concat(
                [
                    sanitize_dataset_rows(
                        validate_dataframe(pl.read_parquet(path), dataset), dataset
                    )
                    for path in existing_files
                ],
                how="diagonal_relaxed",
            )
            existing = _pit(existing)
            combined = pl.concat([existing, combined], how="diagonal_relaxed")
            if pk:
                combined = _dedupe_storage(combined, dataset)
        out_dir.mkdir(parents=True, exist_ok=True)
        business_changed = not _business_equal(existing, combined)
        if change_log is not None and business_changed:
            change_log.append({"partition": None, **summarize_changes(existing, combined, dataset)})
        # One-time migration: a file written before the bitemporal columns
        # existed keeps deriving them on every read until it is rewritten, and
        # the digest above cannot see that because both sides are normalized.
        pit_missing = _pit_columns_absent(out_path, dataset)
        should_write = business_changed or had_fragments or pit_missing or not out_path.is_file()
        if should_write:
            write_parquet_atomic(out_path, combined, compression="zstd")
            for stale in out_dir.rglob("*.parquet"):
                if stale != out_path:
                    stale.unlink()
        if changed_files is not None and (
            business_changed or had_fragments or before_digest is None
        ):
            changed_files.append(out_path)
        return combined.height

    _PART = "__partition__"
    combined = combined.with_columns(
        _partition_values(combined, partition_col, granularity).alias(_PART)
    )

    total = 0
    for key, group in combined.partition_by(_PART, as_dict=True).items():
        val_str = str(key[0] if isinstance(key, tuple) else key)
        group = group.drop(_PART)
        existing_dir = curated.partition_path(dataset, partition_col, val_str)
        read_partition_dir = read_root / f"{partition_col}={val_str}"
        out_path = existing_dir / "part-merged.parquet"
        before_digest = sha256_file(out_path) if out_path.is_file() else None
        had_fragments = False
        frames = [group]
        existing = pl.DataFrame(schema=group.schema)
        if read_partition_dir.exists():
            existing_files = []
            for existing_path in read_partition_dir.rglob("*.parquet"):
                # A committed generation has no output path in the mutable
                # destination, so every source file is a legitimate fragment.
                had_fragments = had_fragments or existing_path.name != "part-merged.parquet"
                existing_files.append(
                    sanitize_dataset_rows(
                        validate_dataframe(pl.read_parquet(existing_path), dataset), dataset
                    )
                )
            if existing_files:
                existing = pl.concat(existing_files, how="diagonal_relaxed")
                if pk:
                    existing = _dedupe_storage(existing, dataset)
                existing = _pit(existing)
                frames.append(existing)
        merged = pl.concat(frames, how="diagonal_relaxed")
        if pk:
            merged = _dedupe_storage(merged, dataset)
        merged = _pit(merged)
        business_changed = not _business_equal(existing, merged)
        if change_log is not None and business_changed:
            change_log.append(
                {
                    "partition": f"{partition_col}={val_str}",
                    **summarize_changes(existing, merged, dataset),
                }
            )
        # See the unpartitioned branch: rewrite once so the columns stop being
        # recomputed on every read.
        pit_missing = _pit_columns_absent(out_path, dataset)
        should_write = business_changed or had_fragments or pit_missing or not out_path.is_file()
        if should_write:
            written = curated.write_partition(
                dataset, partition_col, val_str, merged, "part-merged.parquet"
            )
        else:
            # Keep the existing inode untouched when reconciliation changed
            # only fetched_at; this is the physical no-op that prevents a
            # spurious changed_files entry and revision.
            written = out_path
        if changed_files is not None and (
            business_changed or had_fragments or before_digest is None
        ):
            changed_files.append(written)
        total += merged.height
    return total
