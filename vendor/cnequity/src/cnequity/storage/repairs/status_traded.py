"""Fill missing status keys from actual prints, without claiming historical ST."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import polars as pl

from cnequity.config import Config
from cnequity.domain.contracts import contract_fingerprint, dataset_contract
from cnequity.domain.schemas import with_provenance
from cnequity.domain.trading_status import DERIVED_TRADED_SOURCE, STATUS_NORMAL
from cnequity.file_lock import lake_mutation_lock
from cnequity.quality.publication import check_repair_publication
from cnequity.quality.trading_status_coverage import KEY, traded_status_evidence
from cnequity.storage.parquet import StagingWriter, compact_dataset
from cnequity.storage.read_context import ReadContext
from cnequity.storage.revisions import RevisionStore


def repair_status_traded(config: Config, *, start: date, end: date, apply: bool = False) -> dict:
    """Preview without writes, or publish additive rows under the mutation lock.

    Apply recomputes the candidates against the then-current inputs. Existing
    status keys, including contradictions, are never replaced. Old revisions
    stay readable. An ingestion still writing staging cannot change these
    committed inputs while this function holds the publication lock.
    """
    if end < start:
        raise ValueError("end must not be before start")
    with lake_mutation_lock(config.meta_root) if apply else nullcontext():
        store = RevisionStore(
            config.meta_root, config.curated_root, config.derived_root, create=apply
        )
        if apply:
            for dataset in ("daily_bars", "instruments", "trading_status"):
                store.ensure_current(dataset)
        context = ReadContext.capture(config, ("daily_bars", "instruments", "trading_status"), {})
        evidence = traded_status_evidence(config, start=start, end=end, read_context=context)
        missing = evidence.filter(pl.col("exclusion").is_null() & ~pl.col("has_status"))
        pairs = missing.select(*KEY, "asset_class").sort(KEY).collect()
        excluded = evidence.filter(pl.col("exclusion").is_not_null()).group_by("exclusion")
        exclusions = dict(excluded.len().sort("exclusion").collect().iter_rows())
        report = {
            "applied": False,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": pairs.height,
            "symbols": pairs["symbol"].n_unique(),
            "source": DERIVED_TRADED_SOURCE,
            "risk_warning": None,
            "by_class": dict(pairs.group_by("asset_class").len().sort("asset_class").iter_rows()),
            "by_date": {
                str(day): count
                for day, count in pairs.group_by("trade_date").len().sort("trade_date").iter_rows()
            },
            "exclusions": exclusions,
            "sample": pairs.head(20).with_columns(pl.col("trade_date").cast(pl.String)).to_dicts(),
            "input_roots": {name: str(root) for name, root in context.roots.items()},
        }
        if not apply or pairs.is_empty():
            return report
        rows = with_provenance(
            pairs.select(KEY).with_columns(
                pl.lit(True).alias("is_trading"),
                pl.lit(STATUS_NORMAL).alias("status"),
                pl.lit(None, dtype=pl.Boolean).alias("risk_warning"),
            ),
            source=DERIVED_TRADED_SOURCE,
            data_version="v1",
        )
        run_id = f"status-traded-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        store.materialize_current("trading_status")
        changed: list[Path] = []
        # Isolated staging cannot be swept into another ingestion or retry.
        with TemporaryDirectory(prefix="cne-status-traded-") as directory:
            staging = Path(directory)
            StagingWriter(staging).write_batch("trading_status", run_id, "repair-0", rows)
            compact_dataset(
                staging,
                config.curated_root,
                "trading_status",
                run_id,
                changed_files=changed,
                base_root=context.roots["trading_status"],
            )
        publication = check_repair_publication(config, "trading_status", run_id, changed)
        contract = dataset_contract("trading_status")
        revision = store.commit(
            "trading_status",
            run_id=run_id,
            changed_files=changed,
            schema_version=int(contract["schema_version"]),
            contract_fingerprint=contract_fingerprint(contract),
            metadata={
                "reason": "status_traded",
                "rows_added": pairs.height,
                "input_roots": report["input_roots"],
                "publication_audit": publication["report_path"],
            },
        )
        if revision is None:
            raise RuntimeError("status_traded produced no revision")
        report.update(
            applied=True, revision=revision.revision, publication_audit=publication["report_path"]
        )
        return report
