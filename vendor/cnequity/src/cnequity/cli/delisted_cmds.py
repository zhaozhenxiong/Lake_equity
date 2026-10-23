"""Read the delisted catalogue with `delisted status`.

Rebuilding the catalogue is a one-off project in `scripts/delisted_ops.py`.
Reading it stays here; history fetching uses `backfill daily_bars`.
"""

from __future__ import annotations

import json

import click

from cnequity.cli._root import cli, moved_hints
from cnequity.cli._shared import (
    _cfg,
    config_option,
    parse_date_option,
)
from cnequity.i18n import N_

# Exit 0 even when sources are down. A red source is this command's *output*,
# not its failure.


@cli.group(
    "delisted",
    cls=moved_hints({"backfill": "cne backfill daily_bars --profile delisted --start <date>"}),
)
def delisted_grp():
    """Read the delisted directory; history is backfilled with backfill daily_bars.

    \b
    Rebuilding the directory itself (code-space scans, terminal-state reconciliation,
    instruments repairs and the coverage gate) is a one-off engineering job, not routine
    operations, and lives in `scripts/delisted_ops.py`.

    """


@delisted_grp.command("status")
@config_option
@click.option(
    "--since", default="2016-01-01", show_default=True, help=N_("Start of the lake window.")
)
@click.option("--sample", default=15, show_default=True, help=N_("Number of detail rows to print."))
def delisted_status(config_path: str, since: str, sample: int):
    """Summarize the directory: how many, since when, and how many are still to probe."""
    from collections import Counter

    from cnequity.steps.delisted import (
        LIVE_RECENCY_DAYS,
        classify_catalog,
        delisted_symbols_in_window,
        pending_codes,
    )

    cfg = _cfg(config_path)
    start = parse_date_option(since, "--since")
    catalog, live_missing = classify_catalog(cfg)
    in_window = {s: d for s, d in catalog.items() if d >= start}
    by_year = Counter(d.year for d in in_window.values())
    by_board = Counter(s.split(".")[1] for s in in_window)
    recent = sorted(in_window.items(), key=lambda kv: kv[1], reverse=True)[:sample]
    click.echo(
        json.dumps(
            {
                "delisted": len(catalog),
                "in_window": len(in_window),
                # Still quoting near the latest session: either a code the
                # instrument list is missing, or a delisting inside the recency
                # window that will reclassify on the next sweep.
                "live_or_recent": len(live_missing),
                "live_or_recent_by_exchange": dict(
                    sorted(Counter(s.split(".")[1] for s in live_missing).items())
                ),
                "live_recency_days": LIVE_RECENCY_DAYS,
                "window_start": since,
                "pending_probe": len(pending_codes(cfg)),
                "not_yet_ingested": len(delisted_symbols_in_window(cfg, start)),
                "by_year": dict(sorted(by_year.items())),
                "by_exchange": dict(sorted(by_board.items())),
                "most_recent": [{"symbol": s, "last_traded": d.isoformat()} for s, d in recent],
            },
            indent=2,
        )
    )
