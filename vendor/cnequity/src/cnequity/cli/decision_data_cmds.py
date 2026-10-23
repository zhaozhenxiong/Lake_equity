"""Research-data evidence inventory commands."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import click

from cnequity.cli._root import cli
from cnequity.cli._shared import _cfg, config_option
from cnequity.i18n import N_
from cnequity.quality.cash_rights import reviewed_rights_inventory, save_reviewed_rights
from cnequity.quality.decision_gaps import (
    check_research_window,
    inventory,
    save_immutable,
    stock_term_diagnostic,
)

_START = click.option(
    "--start", type=click.DateTime(formats=["%Y-%m-%d"]), default="2016-01-01", show_default=True
)
_END = click.option(
    "--end",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help=N_(
        "Defaults to today; when [research] holdout_start is configured, defaults to the day before it."
    ),
)
_OUTPUT = click.option("--output-dir", type=click.Path(path_type=Path), default=None)


def _window(cfg, start, end) -> tuple[date, date]:
    """Resolve the window and refuse it before anything reads the lake."""
    holdout = cfg.research_holdout_start
    start_day = start.date()
    if end is not None:
        end_day = end.date()
    else:
        end_day = date.today() if holdout is None else min(date.today(), holdout - timedelta(1))
    try:
        check_research_window(start_day, end_day, holdout_start=holdout)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--start/--end") from exc
    return start_day, end_day


def _echo(payload: dict) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


@cli.group("decision-data")
def decision_data() -> None:
    """Inspect historical decision-data evidence and unresolved gaps."""


@decision_data.command("payment-gaps")
@_START
@_END
@_OUTPUT
@config_option
def payment_gaps(start, end, output_dir: Path | None, config_path: str) -> None:
    """Freeze missing cash-payment dates, split by tradable span and ex-date rule."""
    cfg = _cfg(config_path)
    start_day, end_day = _window(cfg, start, end)
    result = inventory(
        data_root=cfg.data_root,
        start=start_day,
        end=end_day,
        holdout_start=cfg.research_holdout_start,
    )
    target = save_immutable(result, output_dir or cfg.meta_root / "decision_data_gaps")
    _echo(
        {
            "path": str(target),
            "count": result["count"],
            "source_route_counts": result["source_route_counts"],
            "bar_relation_counts": result["bar_relation_counts"],
            "within_span_count": result["within_span_count"],
            "within_span_rule_ineligible_count": result["within_span_rule_ineligible_count"],
            "revision_id": result["revision_id"],
        }
    )


@decision_data.command("stock-terms")
@_START
@_END
@_OUTPUT
@config_option
def stock_terms(start, end, output_dir: Path | None, config_path: str) -> None:
    """Freeze same-day bonus/transfer candidates for issuer reconciliation."""
    cfg = _cfg(config_path)
    start_day, end_day = _window(cfg, start, end)
    result = stock_term_diagnostic(
        data_root=cfg.data_root,
        start=start_day,
        end=end_day,
        holdout_start=cfg.research_holdout_start,
    )
    target = save_immutable(
        result, output_dir or cfg.meta_root / "decision_data_gaps", prefix="stock-terms"
    )
    _echo(
        {
            "path": str(target),
            "count": result["count"],
            "revision_id": result["corporate_actions_revision_id"],
            "coexistence_is_not_error_proof": True,
        }
    )


@decision_data.command("cash-rights")
@_START
@_END
@_OUTPUT
@config_option
def cash_rights(start, end, output_dir: Path | None, config_path: str) -> None:
    """Export reviewed holder-specific rights tied to the current action revision."""
    cfg = _cfg(config_path)
    start_day, end_day = _window(cfg, start, end)
    result = reviewed_rights_inventory(
        data_root=cfg.data_root,
        start=start_day,
        end=end_day,
        holdout_start=cfg.research_holdout_start,
    )
    target = save_reviewed_rights(result, output_dir or cfg.meta_root / "decision_cash_rights")
    _echo(
        {
            "path": str(target),
            "count": result["count"],
            "event_count": result["event_count"],
            "revision_id": result["corporate_actions_revision_id"],
        }
    )
