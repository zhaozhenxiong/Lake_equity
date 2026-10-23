"""Repairs of stored data, each published as a new retained revision."""

from __future__ import annotations

import json

import click

from cnequity.cli._root import CneCommand, cli
from cnequity.cli._shared import _cfg, comma_option, config_option, warn_deprecated_migration
from cnequity.i18n import N_, _


def _emit(value: dict) -> None:
    click.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


class DeprecatedMigration(CneCommand):
    """A repair that only rewrites what a writer fixed in 0.17 or earlier left behind.

    Ingestion no longer produces these defects, so a lake built or brought up
    to date by a current version has nothing for them to do. They stay for two
    minor releases so an older lake can still run them, and are removed in 0.20.0.
    Click's own `deprecated=` is not used: its label and warning come from
    Click's catalog, which has no Chinese. The warning goes to stderr so the
    JSON on stdout stays parseable.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.epilog = N_(
            "Deprecated: a one-time migration for lakes written by cnequity 0.17 or earlier; "
            "current versions no longer write this defect. It will be removed in 0.20.0."
        )

    def get_short_help_str(self, limit: int = 45) -> str:
        label = _("[deprecated] ")
        return label + super().get_short_help_str(limit=max(limit - len(label), 10))

    def invoke(self, ctx: click.Context):
        warn_deprecated_migration(ctx.command_path)
        return super().invoke(ctx)


@cli.group()
def repair():
    """Repair stored data: previews by default; --apply verifies, publishes, and keeps the old version."""


@repair.command("missing-values", cls=DeprecatedMigration)
@click.argument("dataset", type=click.Choice(["valuation_metrics", "index_constituents"]))
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Replace missing-value zero placeholders with nulls and publish a retained revision; preview only by default."
    ),
)
def missing_values(dataset: str, config_path: str, apply: bool):
    """Fix valuation zero placeholders and index weights missing from EastMoney and CNI, keeping the old version."""
    from cnequity.storage.repairs.missing_values import repair_missing_values

    _emit(repair_missing_values(_cfg(config_path), dataset, apply=apply))


@repair.command("financial-announcements", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Append explicitly marked original-disclosure reconstructions; preview only by default."
    ),
)
def financial_announcements(config_path: str, apply: bool):
    """Add financial-statement reconstructions based on LICO original disclosure dates, keeping the actually disclosed version and the old lake version."""
    from cnequity.storage.repairs.financial_announcements import repair_financial_announcements

    _emit(repair_financial_announcements(_cfg(config_path), apply=apply))


@repair.command("fund-volume")
@config_option
@click.option(
    "--apply", is_flag=True, help=N_("Replace verified whole rows and publish a retained revision.")
)
@click.option(
    "--refresh-sources",
    is_flag=True,
    help=N_(
        "Archive official historical K-lines and any required independent turnover evidence; replay archives offline by default."
    ),
)
@click.option("--max-symbols", type=click.IntRange(min=1), default=16, show_default=True)
def fund_volume(config_path: str, apply: bool, refresh_sources: bool, max_symbols: int):
    """Repair Shanghai/Shenzhen exchange-traded funds with zero volume but positive turnover; must agree with official daily prices and amounts."""
    from cnequity.storage.repairs.fund_volume import repair_fund_volume

    try:
        _emit(
            repair_fund_volume(
                _cfg(config_path),
                apply=apply,
                refresh_sources=refresh_sources,
                max_symbols=max_symbols,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@repair.command("financial-originals")
@config_option
@comma_option("--symbols", help=N_("Comma-separated stock symbols."))
@click.option(
    "--all-stocks", is_flag=True, help=N_("Use the lake stock catalogue; choose this or --symbols.")
)
@click.option(
    "--after",
    help=N_("Resume from the previous next_cursor (YEAR:SYMBOL); retry failures without a cursor."),
)
@click.option(
    "--quarter",
    type=click.Choice(["Q1", "Q2", "Q3", "Q4"]),
    default="Q4",
    show_default=True,
    help=N_(
        "Report period: Q2 is half-year and Q4 annual; income and cash flow use year-to-date figures only."
    ),
)
@click.option("--start-year", type=click.IntRange(min=2001), default=2016, show_default=True)
@click.option("--end-year", type=click.IntRange(min=2001), default=2024, show_default=True)
@click.option("--max-reports", type=click.IntRange(min=1), default=16, show_default=True)
@click.option(
    "--reparse",
    is_flag=True,
    help=N_(
        "Reparse existing original reports; unchanged facts retain their original observation times."
    ),
)
@click.option(
    "--refresh-sources",
    is_flag=True,
    help=N_(
        "Fetch complete CNINFO announcement queries and original periodic reports; replay archived sources by default."
    ),
)
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Append verifiable current-year figures from original reports; retain existing vintages and honest observation times."
    ),
)
def financial_originals(
    config_path: str,
    symbols: str | None,
    all_stocks: bool,
    after: str | None,
    quarter: str,
    start_year: int,
    end_year: int,
    max_reports: int,
    reparse: bool,
    refresh_sources: bool,
    apply: bool,
):
    """Recover consolidated statement items from original periodic reports; does not use comparative-period figures, and does not pass off today's collection as a historical observation."""
    from cnequity.cli._shared import comma_values
    from cnequity.storage.repairs.financial_originals import repair_financial_originals

    try:
        _emit(
            repair_financial_originals(
                _cfg(config_path),
                symbols=comma_values(symbols, "--symbols") if symbols else None,
                all_stocks=all_stocks,
                after=after,
                quarter=int(quarter[-1]),
                start_year=start_year,
                end_year=end_year,
                max_reports=max_reports,
                reparse=reparse,
                refresh_sources=refresh_sources,
                apply=apply,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@repair.command("daily-bar-coverage", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Retire BSE session debts and proven pre-listing debts using committed data; preview by default."
    ),
)
def daily_bar_coverage(config_path: str, apply: bool):
    """Re-check Beijing whole-day and pre-listing debts, clearing them only on committed, non-contradictory evidence."""
    from cnequity.storage.repairs.daily_bar_coverage import repair_daily_bar_coverage

    _emit(repair_daily_bar_coverage(_cfg(config_path), apply=apply))


@repair.command("fund-no-print")
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Approve fund auction-bar coverage evidence and retire corresponding debts without creating bars or halt rows."
    ),
)
@click.option(
    "--refresh-sources",
    is_flag=True,
    help=N_("Fetch historical SSE/SZSE fund reports; replays retained reports offline by default."),
)
@click.option(
    "--max-sessions",
    type=click.IntRange(min=1),
    default=32,
    show_default=True,
    help=N_("Maximum owed trading sessions to review per batch."),
)
def fund_no_print(config_path: str, apply: bool, refresh_sources: bool, max_sessions: int):
    """Check debts against explicit fund auction daily-bar coverage evidence from the Shanghai and Shenzhen exchanges; being tradable does not mean there was an auction trade that day."""
    from cnequity.storage.repairs.fund_no_print import repair_fund_no_print

    _emit(
        repair_fund_no_print(
            _cfg(config_path),
            apply=apply,
            refresh_sources=refresh_sources,
            max_sessions=max_sessions,
        )
    )


@repair.command("fund-factors", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Publish corrected factors from recorded split and transfer terms, retaining the old revision; preview by default."
    ),
)
def fund_factors(config_path: str, apply: bool):
    """Add the share conversions ETF/LOF factors missed, keeping other sources' steps and the cash-dividend basis."""
    from cnequity.storage.repairs.fund_factors import repair_fund_factors

    _emit(repair_fund_factors(_cfg(config_path), apply=apply))


@repair.command("adj-factors")
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Publish supported factor corrections and retain old revisions; unresolved events remain advisory."
    ),
)
@click.option(
    "--refresh-sources",
    is_flag=True,
    help=N_("Fetch independent factor evidence; uses local caches only by default."),
)
@comma_option(
    "--symbols",
    default=None,
    help=N_("Comma-separated symbols; checks historical issues in bounded batches by default."),
)
@click.option(
    "--start",
    default=None,
    help=N_("Start of the event window (YYYY-MM-DD); cumulative effects continue afterward."),
)
@click.option(
    "--end",
    default=None,
    help=N_("End of the event window (YYYY-MM-DD); cumulative effects continue afterward."),
)
@click.option(
    "--max-symbols",
    default=100,
    type=click.IntRange(min=1),
    show_default=True,
    help=N_("Maximum symbols per batch; saves historical progress after applying."),
)
@click.option(
    "--plan",
    default=None,
    help=N_("Require the specified preview plan ID; changed inputs require a new preview."),
)
def adj_factors(config_path, apply, refresh_sources, symbols, start, end, max_symbols, plan):
    """Repair stock factors and ETF/LOF share conversions together; previews offline and saves a plan by default."""
    from cnequity.cli._shared import comma_values, parse_date_option
    from cnequity.storage.repairs.adj_factors import repair_adj_factors

    try:
        _emit(
            repair_adj_factors(
                _cfg(config_path),
                apply=apply,
                refresh_sources=refresh_sources,
                symbols=comma_values(symbols, "--symbols"),
                start=parse_date_option(start, "--start"),
                end=parse_date_option(end, "--end"),
                max_symbols=max_symbols,
                plan_id=plan,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@repair.command("fund-listing-dates", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Archive exchange directories and publish missing fund listing dates; preview by default."
    ),
)
def fund_listing_dates(config_path: str, apply: bool):
    """Fill listing dates from the exchanges' current ETF/LOF directories; previewing also queries the sources."""
    from cnequity.storage.repairs.fund_listing_dates import repair_fund_listing_dates

    _emit(repair_fund_listing_dates(_cfg(config_path), apply=apply))


@repair.command("index-snapshots")
@config_option
@comma_option(
    "--indices",
    default=None,
    help=N_(
        "Comma-separated indices; defaults to CSI 300, STAR 50, Shenzhen Component and ChiNext."
    ),
)
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Replace complete index snapshots using their actual file dates; retain old revisions."
    ),
)
@click.option(
    "--refresh-sources",
    is_flag=True,
    help=N_("Fetch official constituent and weight files; replay offline by default."),
)
def index_snapshots(config_path: str, indices: str | None, apply: bool, refresh_sources: bool):
    """Recover the official dated constituents and real weights that are obtainable; does not project backwards or fill weights across dates."""
    from cnequity.cli._shared import comma_values
    from cnequity.storage.repairs.index_snapshots import repair_index_snapshots

    try:
        _emit(
            repair_index_snapshots(
                _cfg(config_path),
                indices=comma_values(indices, "--indices"),
                apply=apply,
                refresh_sources=refresh_sources,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@repair.command("index-history")
@config_option
@click.option(
    "--index-symbol",
    type=click.Choice(["399001.SZ", "000688.SH"]),
    default="399001.SZ",
    show_default=True,
    help=N_("Select a reviewed historical membership catalog."),
)
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Replace whole membership history within the reviewed range; retain official dated weights and old revisions."
    ),
)
@click.option(
    "--refresh-sources",
    is_flag=True,
    help=N_(
        "Archive missing official historical attachments from the catalog; replay offline by default."
    ),
)
def index_history(config_path: str, index_symbol: str, apply: bool, refresh_sources: bool):
    """Replay the verified historical announcements of the SZSE Component Index or STAR 50; each effective date stores the full list, with unknown historical weights left empty."""
    from cnequity.storage.repairs.index_history import repair_index_history

    try:
        _emit(
            repair_index_history(
                _cfg(config_path),
                index_symbol=index_symbol,
                apply=apply,
                refresh_sources=refresh_sources,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@repair.command("stock-listing-dates")
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Publish stock listing dates confirmed by the official IPO calendar; retain old revisions."
    ),
)
@click.option(
    "--refresh-sources",
    is_flag=True,
    help=N_("Fetch CNINFO IPO calendars; replay archived sources by default."),
)
@click.option("--max-sessions", type=click.IntRange(min=1), default=32, show_default=True)
@click.option(
    "--event-date",
    multiple=True,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help=N_(
        "Replay official listing events for an additional date; repeatable and usable before a stock has daily bars."
    ),
)
def stock_listing_dates(
    config_path: str, apply: bool, refresh_sources: bool, max_sessions: int, event_date: tuple
):
    """Fill stock listing dates confirmed by the official calendar; the first daily bar or an explicit date only locates the request."""
    from cnequity.storage.repairs.stock_listing_dates import repair_stock_listing_dates

    _emit(
        repair_stock_listing_dates(
            _cfg(config_path),
            apply=apply,
            refresh_sources=refresh_sources,
            max_sessions=max_sessions,
            event_dates=tuple(day.date() for day in event_date),
        )
    )


@repair.command("layout")
@click.argument("dataset")
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_("Write and publish a new version; by default only prints the plan."),
)
def layout(dataset: str, config_path: str, apply: bool):
    """Merge files under a partitioned dataset's root or a wrong-key directory into the registered partitions.

    \b
    Several observations of one primary key only fill each other's blanks when their non-null values agree exactly; conflicting keys keep one whole row by the canonical rules.
    All original observations of duplicated keys are written to _quarantine as evidence, and old versions are kept.

    """
    from cnequity.storage.repairs.layout import LayoutRepairError, repair_layout

    try:
        _emit(repair_layout(_cfg(config_path), dataset.lower(), apply=apply))
    except LayoutRepairError as exc:
        raise click.ClickException(str(exc)) from exc


@repair.command("valuation-basis", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_("Write and publish a new version; by default only prints the plan."),
)
def valuation_basis(config_path: str, apply: bool):
    """Unify the P/E and market-cap bases of valuation_metrics.

    \b
    push2 dynamic P/E moves into pe_dynamic; Baostock float market cap is converted from the average-price basis to the close basis,
    with values that cannot be checked kept as they are and marked as the vwap basis; total market cap is rebuilt from the total shares in
    effect that day in share_structure, and rows with no share record keep the year-end share estimate and are marked. Rows that already have a basis are not recomputed.

    """
    from cnequity.storage.repairs.valuation_basis import repair_valuation_basis

    _emit(repair_valuation_basis(_cfg(config_path), apply=apply))


@repair.command("corporate-action-gaps")
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Fetch from Baostock and publish a new version; by default only prints the plan offline."
    ),
)
def corporate_action_gaps(config_path: str, apply: bool):
    """Add the ex-rights events where both the Sina and Baostock factors step but the lake has no record.

    \b
    Based on the evidence of the latest `cne derive adj_factor_source`. A record within 10 days whose date is off, and whose
    terms explain the step, moves to the step date; the rest fetch dividends from Baostock by security and year,
    accepting only rows whose ex-date is exactly the step date and whose terms explain the step. What cannot be explained stays a gap and is never invented.

    """
    from cnequity.storage.repairs.corporate_action_gaps import repair_corporate_action_gaps

    try:
        _emit(repair_corporate_action_gaps(_cfg(config_path), apply=apply))
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@repair.command("orphan-symbols", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_("Publish a new version after verification; by default only prints the plan."),
)
def orphan_symbols(config_path: str, apply: bool):
    """Delete rows in adj_factors and corporate_actions for securities that never appear in daily_bars.

    \b
    These rows have no prices to adjust: quotes of off-exchange funds (519xxx) that once came in as NAV series were cleaned up,
    but their factors and dividends stayed; listed funds whose quotes are not collected are among them too. They only ever get reported as factor and corporate-action conflicts.
    Each dataset publishes a new version, and old versions are kept and readable by version.

    """
    from cnequity.storage.repairs.orphan_symbols import repair_orphan_symbols

    _emit(repair_orphan_symbols(_cfg(config_path), apply=apply))


@repair.command("stale-suspensions", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_("Publish a new version after verification; by default only prints the plan."),
)
def stale_suspensions(config_path: str, apply: bool):
    """Delete inferred suspensions that actual trading bars contradict (rows from the derived_bar_gap source in trading_status).

    \b
    These rows are inferred from missing daily bars; when a run missed quotes, the gap was recorded as a suspension by mistake.
    Once the quotes are filled, a bar with trades on the same day proves the row wrong. Suspension records from independent sources are unaffected.

    """
    from cnequity.storage.repairs.orphan_symbols import repair_stale_derived_suspensions

    _emit(repair_stale_derived_suspensions(_cfg(config_path), apply=apply))


@repair.command("status-outages", cls=DeprecatedMigration)
@config_option
@click.option("--start", required=True, help=N_("Window start (YYYY-MM-DD)."))
@click.option("--end", required=True, help=N_("Window end (YYYY-MM-DD)."))
@click.option(
    "--apply",
    is_flag=True,
    help=N_("Publish a new version after verification; by default only prints the plan."),
)
def status_outages(config_path: str, start: str, end: str, apply: bool):
    """Rebuild the trading_status cache rows (eastmoney_cached) from the EastMoney outage by the current rules.

    \b
    The old rules reused rows the cache wrote itself; a long holiday expired the cache and recorded securities without records as suspended.
    All cache rows in the window are deleted; on exchanges whose primary source did not answer that day, securities with no other record reuse
    the latest first-hand record within the previous 5 sessions, keeping its original fetch time, while Baostock history and suspensions inferred from
    daily-bar gaps still take precedence. Securities with no first-hand record get no row.

    """
    from datetime import date

    from cnequity.storage.repairs.status_outages import repair_status_outages

    try:
        window = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    _emit(repair_status_outages(_cfg(config_path), start=window[0], end=window[1], apply=apply))


@repair.command("status-listing-window", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_("Publish a new version after verification; by default only prints the plan."),
)
def status_listing_window(config_path: str, apply: bool):
    """Delete trading_status rows that fall before a security's listing date, or that no delisting record supports.

    \b
    An old merge once inferred new securities with no daily bars yet as delisted, and the status step then wrote derived_delisted for their
    first few sessions after listing; EastMoney also reports codes not yet listed as trading normally.
    A derived_delisted row is kept only while the current instruments still records a delisting date no earlier than the listing date and no later
    than the row; rows before the listing date are always deleted, unless daily bars show trades before the listing date. No rows are added.

    """
    from cnequity.storage.repairs.status_listing_window import repair_status_listing_window

    _emit(repair_status_listing_window(_cfg(config_path), apply=apply))


@repair.command("status-traded")
@config_option
@click.option(
    "--start",
    required=True,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help=N_("Start of the traded-status repair window (YYYY-MM-DD)."),
)
@click.option(
    "--end",
    required=True,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help=N_("End of the traded-status repair window (YYYY-MM-DD)."),
)
@click.option(
    "--apply",
    is_flag=True,
    help=N_("Publish a new version after verification; by default only prints the plan."),
)
def status_traded(config_path: str, start, end, apply: bool):
    """Fill missing status keys from actual trading bars, leaving ST unknown.

    \b
    Only fills stock/ETF keys within known listing periods that have volume above zero and no status row, with source derived_traded.
    Does not overwrite existing status, does not read days without trades as suspensions, and does not create ST coverage evidence.
    The default preview writes nothing; --apply re-checks the current version, then publishes, keeping the old version.

    """
    from cnequity.storage.repairs.status_traded import repair_status_traded

    if end < start:
        raise click.BadParameter(_("--end must be on or after --start"))
    _emit(repair_status_traded(_cfg(config_path), start=start.date(), end=end.date(), apply=apply))


@repair.command("misdated-valuation", cls=DeprecatedMigration)
@config_option
@click.option(
    "--apply",
    is_flag=True,
    help=N_(
        "Fetch from the EastMoney datacenter and publish a new version; by default only prints the plan offline."
    ),
)
def misdated_valuation(config_path: str, apply: bool):
    """Replace push2 snapshot rows in valuation_metrics stamped with the wrong session.

    \b
    push2 snapshots carry no date and only describe the latest closed session at fetch time. Rows whose session at fetch time
    disagrees with the date on the row are deleted with --apply and replaced in the same version with valuations EastMoney datacenter
    publishes by date; keys datacenter lacks no longer keep the wrong row.

    """
    from cnequity.storage.repairs.valuation_snapshots import repair_misdated_valuation

    _emit(repair_misdated_valuation(_cfg(config_path), apply=apply))
