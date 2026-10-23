"""Replace vendor placeholders with nulls in a retained, gated revision."""

from contextlib import nullcontext

import polars as pl

from cnequity.config import Config
from cnequity.domain.membership import null_missing_index_weights
from cnequity.domain.valuation import VALUATION_VALUE_COLUMNS, null_missing_valuations
from cnequity.file_lock import lake_mutation_lock
from cnequity.query.parquet_scan import dataset_has_parquet, scan_parquet_root
from cnequity.storage.read_context import ReadContext
from cnequity.storage.repairs.orphan_symbols import _publish
from cnequity.storage.revisions import RevisionStore

DATASETS = {"valuation_metrics": "trade_date", "index_constituents": "as_of_date"}


def repair_missing_values(config: Config, dataset: str, *, apply: bool = False) -> dict:
    if dataset not in DATASETS:
        raise ValueError(f"unsupported missing-value repair: {dataset}")
    with lake_mutation_lock(config.meta_root) if apply else nullcontext():
        store = RevisionStore(
            config.meta_root, config.curated_root, config.derived_root, create=apply
        )
        if apply:
            store.ensure_current(dataset)
        context = ReadContext.capture(config, (dataset,), {})
        root = context.roots[dataset]
        report = {"dataset": dataset, "applied": False, "rows": 0, "fields": {}}
        if not dataset_has_parquet(root, committed=False):
            return report
        columns = VALUATION_VALUE_COLUMNS if dataset == "valuation_metrics" else ("weight",)
        frame = scan_parquet_root(root, committed=False)
        if dataset == "index_constituents":
            report["coverage"] = (
                frame.group_by("index_symbol", "source")
                .agg(
                    pl.col("as_of_date").min().alias("first_snapshot"),
                    pl.col("as_of_date").max().alias("last_snapshot"),
                    pl.col("as_of_date").n_unique().alias("snapshots"),
                )
                .sort("index_symbol", "source")
                .collect()
                .to_dicts()
            )
        predicates = {
            column: (pl.col(column) == 0).fill_null(False)
            for column in columns
            if column in frame.collect_schema().names()
        }
        if dataset == "index_constituents":
            predicates["weight"] &= pl.col("source").is_in(["eastmoney", "cni"])
        counts = (
            frame.select(
                pl.any_horizontal(list(predicates.values())).sum().alias("rows"),
                *(predicate.sum().alias(column) for column, predicate in predicates.items()),
            )
            .collect()
            .row(0, named=True)
        )
        report.update(rows=counts.pop("rows"), fields=counts)
        if not apply or not report["rows"]:
            return report
        report.update(
            _publish(
                config,
                store,
                dataset,
                DATASETS[dataset],
                root,
                keep=null_missing_valuations
                if dataset == "valuation_metrics"
                else null_missing_index_weights,
                reason="missing_value_placeholders",
                metadata={"fields": counts},
            )
        )
        report["applied"] = True
        return report
