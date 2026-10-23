"""What `cne init` says when it ends.

The full result is a machine record (phase receipts, per-batch outcomes,
fallback options) that ran to tens of thousands of lines for a full-market
lake. Printed raw it buried the one thing a first-time user needs: whether
the lake is usable and what to run next. That record is saved to a file;
the terminal gets the answer.
"""

from __future__ import annotations

import json
from pathlib import Path

from cnequity.cli.demo import MCP_GUIDE_URL
from cnequity.i18n import N_, _

STEP_LABELS = {
    "instruments": N_("security master"),
    "trading_calendar": N_("trading calendar"),
    "corporate_actions": N_("corporate actions"),
    "daily_bars": N_("daily bars"),
    "index_bars": N_("index daily bars"),
    "trading_status": N_("trading status"),
    "compact": N_("publish to the lake"),
    "derive_adj_factors": N_("adjustment factors"),
    "derive_industry_index": N_("industry indices"),
    "audit": N_("quality audit"),
    "trading_status_derive": N_("suspension derivation"),
}


def save_init_result(log_dir: Path, result: dict) -> Path | None:
    """Keep the full machine result next to the run's log; never fail over it."""
    path = Path(log_dir) / f"cne-init-{result.get('run_id', 'unknown')}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except OSError:
        return None
    return path


def _label(step: str) -> str:
    name = STEP_LABELS.get(step)
    return _("{name} ({step})").format(name=_(name), step=step) if name else step


def render_init_summary(
    result: dict,
    *,
    config_arg: str,
    config_abs: Path,
    data_root: Path,
    log_path: Path | None,
    result_path: Path | None,
) -> list[str]:
    status = str(result.get("status", "failed"))
    run_id = result.get("run_id", "?")
    owed = [str(step) for step in result.get("owed_steps") or []]
    usable = status != "failed" or bool(result.get("lake_usable"))
    resume = f"cne init{config_arg}"

    lines = ["", _("== Lake build result ==")]
    if status != "failed" and not owed:
        lines.append(
            _("✓ The lake is built and ready to use (run {run_id}).").format(run_id=run_id)
        )
    elif usable:
        lines.append(
            _("✓ The lake is built and daily bars can be queried (run {run_id}).").format(
                run_id=run_id
            )
        )
        lines.append(
            _(
                "  These parts did not finish this time, mostly because a data source was temporarily unavailable:"
            )
        )
        lines.extend(f"  - {_label(step)}" for step in owed)
        lines.append(
            _(
                "  Re-run `{resume}` later to fill in only these; finished parts are not redone."
            ).format(resume=resume)
        )
    else:
        lines.append(
            _(
                "✗ The lake build has not finished yet (run {run_id}). Everything already downloaded is saved."
            ).format(run_id=run_id)
        )
        if owed:
            lines.append(_("  Still missing: ") + _(", ").join(_label(step) for step in owed))
        lines.append(
            _(
                "  Once the network or data source is back, re-run `{resume}` to resume where it stopped."
            ).format(resume=resume)
        )
        lines.append(_("  See which data source is unreachable: cne sources probe") + config_arg)
    if log_path is not None:
        lines.append(_("  Log: {log_path}").format(log_path=log_path))
    if result_path is not None:
        lines.append(_("  Full result (JSON): {result_path}").format(result_path=result_path))

    if usable:
        lines += [
            "",
            _("Next steps:"),
            _("  cne check{config_arg}          check that the data is complete and usable").format(
                config_arg=config_arg
            ),
            _("  cne serve{config_arg}          browse the lake in a web page").format(
                config_arg=config_arg
            ),
            _("  cne run daily{config_arg}      run once a day to stay up to date").format(
                config_arg=config_arg
            ),
            "",
            _("Python:"),
            "  from cnequity.query import load",
            f'  bars = load("daily_bars", symbols=["600519.SH"], data_root="{data_root}")',
            "",
            _("Connect an AI (read-only MCP, Claude Code as the example):"),
            f"  claude mcp add cnequity -- cne mcp --config {config_abs}",
            _("Other clients: {MCP_GUIDE_URL}").format(MCP_GUIDE_URL=MCP_GUIDE_URL),
        ]
    return lines
