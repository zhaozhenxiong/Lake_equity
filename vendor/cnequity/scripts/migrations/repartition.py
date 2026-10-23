#!/usr/bin/env python3
"""Rewrite a dataset's partitions at its configured granularity.

Reads work whatever period the directories span — `scan_parquet_root` does not
care how the days are grouped — so this reclaims wasted space and file opens
rather than fixing correctness. That makes it maintenance you run when the
registry's granularity changed under an existing lake, which is a migration,
not a daily operation: it belongs beside `migrate_daily_bars_volume_v2.py` and
`migrate_pit_vintages.py` rather than in the published CLI.

With no dataset and no `--all` it only lists what is out of layout, so the
listing form is always safe to run.

Usage::

    python scripts/migrations/repartition.py                        # what needs it
    python scripts/migrations/repartition.py daily_bars --dry-run   # effect, no swap
    python scripts/migrations/repartition.py daily_bars
    python scripts/migrations/repartition.py --all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cnequity.config import load_config
from cnequity.storage.repairs.repartition import (
    RepartitionError,
    repartition_candidates,
    repartition_dataset,
)

DEFAULT_CONFIG = "configs/cnequity.toml"


def repartition(
    config_path: str,
    *,
    dataset: str | None = None,
    do_all: bool = False,
    dry_run: bool = False,
) -> int:
    if dataset and do_all:
        print("error: pass a dataset or --all, not both", file=sys.stderr)
        return 1

    cfg = load_config(Path(config_path))
    candidates = repartition_candidates(cfg)

    if not dataset and not do_all:
        print(json.dumps({"needs_repartition": candidates}, indent=2))
        return 0

    targets = [dataset] if dataset else candidates
    results = []
    for name in targets:
        try:
            res = repartition_dataset(cfg, name, dry_run=dry_run)
        except RepartitionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        results.append(
            {
                "dataset": res.dataset,
                "changed": res.changed,
                "rows": res.rows,
                "files": f"{res.files_before} -> {res.files_after}",
                "partitions": f"{res.partitions_before} -> {res.partitions_after}",
                "mb": f"{res.bytes_before / 1e6:.1f} -> {res.bytes_after / 1e6:.1f}",
                "mb_saved": round(res.bytes_saved / 1e6, 1),
            }
        )
    print(json.dumps({"dry_run": dry_run, "results": results}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dataset", nargs="?", default=None)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument(
        "--all", dest="do_all", action="store_true", help="Repartition every dataset that needs it."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report the effect without swapping anything."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return repartition(args.config, dataset=args.dataset, do_all=args.do_all, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
