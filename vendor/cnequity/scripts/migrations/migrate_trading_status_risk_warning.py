#!/usr/bin/env python3
"""Rewrite trading_status onto the status / risk_warning split.

`trading_status` used to encode two orthogonal facts in one `status` string.
A security that was both under risk warning and halted could only be one of
them, and the writer let the halt win — so the ST designation was silently
dropped for every suspended session. `status` now carries the trading state
alone (``normal`` / ``suspended`` / ``delisted``) and `risk_warning` carries
the ST / *ST designation. See ``cnequity/domain/trading_status.py``.

This script does two things to stored files:

* adds ``risk_warning``, set from the legacy ``status="st"`` encoding;
* rewrites those rows' ``status`` to ``normal``, which is what they meant —
  a legacy ST row always carried ``is_trading=True``.

It does **not** invent ``delisted`` rows for history. A past session's status
is what was observed then, and back-stamping today's delist dates onto it would
manufacture point-in-time facts the lake never had. Going forward the daily
step writes them; to fill history, re-run the daily step over the window.

Reads are correct with or without this migration — ``validate_dataframe``
upgrades legacy frames on the way in — so this is about making the physical
schema uniform, not about correctness. Running it repeatedly is safe: a file
that already carries the new encoding is not rewritten.

Usage::

    scripts/migrations/migrate_trading_status_risk_warning.py --config configs/cnequity.toml
    scripts/migrations/migrate_trading_status_risk_warning.py --config configs/cnequity.toml --apply

Dry-run is the default; ``--apply`` is required to edit curated files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import polars as pl

from cnequity.config import load_config
from cnequity.domain.trading_status import LEGACY_ST_STATUSES, normalize_legacy
from cnequity.storage.atomic import write_parquet_atomic

DEFAULT_CONFIG = ROOT / "configs/cnequity.toml"


def migrate_frame(df: pl.DataFrame) -> tuple[pl.DataFrame, bool, int]:
    """Return ``(migrated, changed, legacy_st_rows)`` for one status frame."""
    if df.is_empty() or "status" not in df.columns:
        return df, False, 0
    legacy = int(df.filter(pl.col("status").is_in(list(LEGACY_ST_STATUSES))).height)
    migrated = normalize_legacy(df)
    if "risk_warning" in df.columns:
        # Column order is not a difference worth rewriting a file for.
        migrated = migrated.select(df.columns)
    return migrated, not df.equals(migrated), legacy


def run(curated_root: Path, *, apply: bool) -> int:
    root = curated_root / "trading_status"
    files = sorted(root.glob("**/*.parquet")) if root.exists() else []
    if not files:
        print(f"trading_status: no parquet under {root}")
        return 0

    changed = rows = legacy_rows = 0
    for path in files:
        frame = pl.read_parquet(path)
        migrated, file_changed, legacy = migrate_frame(frame)
        rows += frame.height
        legacy_rows += legacy
        if not file_changed:
            continue
        changed += 1
        if apply:
            write_parquet_atomic(path, migrated, compression="zstd")

    verb = "Rewrote" if apply else "Would rewrite"
    print(
        f"{verb} {changed}/{len(files)} trading_status file(s); "
        f"{rows:,} row(s) scanned, {legacy_rows:,} carrying the legacy ST status."
    )
    if not apply:
        print("Dry run — nothing was written. Re-run with --apply to commit.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
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
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    cfg = load_config(args.config)
    print(f"trading_status status/risk_warning split under {cfg.curated_root}")
    return run(cfg.curated_root, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
