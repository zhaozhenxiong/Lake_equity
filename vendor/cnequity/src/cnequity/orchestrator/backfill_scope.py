"""Persist the non-secret fetch scope needed by a later CLI retry."""

from __future__ import annotations

from copy import deepcopy
from datetime import date

from cnequity.config import Config

_REPAIR_FLAGS = {
    "baostock_repair": "_corporate_actions_baostock_repair",
    "ths_repair": "_corporate_actions_ths_repair",
    "eastmoney_bj_repair": "_corporate_actions_eastmoney_bj_repair",
    "bse_tip_repair": "_bse_tip_repair",
    "bj_amount_repair": "_bj_amount_repair",
    "tdx_amount_repair": "_tdx_amount_repair",
    "tdx_volume_repair": "_tdx_volume_repair",
    "turnover_repair": "_turnover_repair",
    "turnover_peer_review": "_turnover_peer_review",
}
_SETTINGS = (
    "ingest_universe",
    "minute_bars_enabled",
    "minute_bars_frequencies",
    "trade_ticks_enabled",
    "trade_ticks_scope",
    "trade_ticks_symbols",
    "trade_ticks_max_symbols",
    "margin_trading_source",
    "futures_enabled",
    "futures_exchanges",
    "futures_options",
    "futures_minute_enabled",
    "futures_minute_products",
    "futures_minute_contracts",
    "futures_minute_max_contracts",
)
_OPTIONAL_SETTINGS = {
    "_backfill_workers": 1,
    "_corporate_actions_payment_repair": False,
    "_corporate_actions_issuer_notice_only": False,
    "_corporate_actions_eastmoney_date_repair": None,
    "_valuation_fill_em_outage": False,
    "_derivatives_refresh": False,
}


def capture_backfill_scope(config: Config) -> dict:
    # Keep the historical keys readable by older tools. Explicitly whitelist
    # operational scope: credentials, proxies and transport policy stay in the
    # current config. A sector --force checkpoint reset is a one-time action;
    # retry resumes that checkpoint instead of resetting it again.
    scope = {
        "start": getattr(config, "_backfill_start", None),
        "end": getattr(config, "_backfill_end", None),
        "symbols": deepcopy(config._backfill_symbols),
        "minute_bars_scope": config.minute_bars_scope,
        "minute_bars_symbols": list(config.minute_bars_symbols),
        **{key: bool(getattr(config, attr, False)) for key, attr in _REPAIR_FLAGS.items()},
        "settings": {
            **{name: deepcopy(getattr(config, name)) for name in _SETTINGS},
            **{
                name: deepcopy(getattr(config, name, default))
                for name, default in _OPTIONAL_SETTINGS.items()
            },
        },
    }
    for key in ("start", "end"):
        if scope[key] is not None:
            scope[key] = scope[key].isoformat()
    dates = scope["settings"]["_corporate_actions_eastmoney_date_repair"]
    if dates is not None:
        scope["settings"]["_corporate_actions_eastmoney_date_repair"] = [
            day.isoformat() for day in dates
        ]
    return scope


def restore_backfill_scope(config: Config, scope: dict) -> None:
    if "minute_bars_scope" in scope:
        config.minute_bars_scope = scope["minute_bars_scope"]
        config.minute_bars_symbols = list(scope.get("minute_bars_symbols") or [])
    for key, attr in (
        ("start", "_backfill_start"),
        ("end", "_backfill_end"),
        ("symbols", "_backfill_symbols"),
    ):
        value = deepcopy(scope.get(key))
        if key in ("start", "end") and value:
            value = date.fromisoformat(value)
        setattr(config, attr, value)
    for key, attr in _REPAIR_FLAGS.items():
        setattr(config, attr, bool(scope.get(key, False)))
    settings = scope.get("settings") or {}
    # Legacy runs did not record these fields; keep their current config.
    # Unknown metadata must never set arbitrary config attributes.
    for name in (*_SETTINGS, *_OPTIONAL_SETTINGS):
        if name not in settings:
            continue
        value = deepcopy(settings[name])
        if name == "_corporate_actions_eastmoney_date_repair" and value is not None:
            value = [date.fromisoformat(day) for day in value]
        setattr(config, name, value)
    config._sector_bars_force = False
