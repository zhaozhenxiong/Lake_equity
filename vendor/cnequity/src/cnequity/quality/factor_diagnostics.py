"""Revision-bound factor diagnostics, never a publication or read gate."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path

import polars as pl

from cnequity.storage.atomic import write_json_atomic
from cnequity.storage.read_context import ReadContext


def _path(config, root: Path) -> Path:
    identity = hashlib.sha256(str(root).encode()).hexdigest()
    return config.meta_root / "quality" / "factor_diagnostics" / f"{identity}.json"


def publish_factor_diagnostics(config, *, symbols=None) -> dict:
    """Archive a full historical observation for the selected factor generation."""
    from cnequity.quality.cross_checks import factor_action_contradictions

    context = ReadContext.capture(config, ("adj_factors", "corporate_actions"), {})
    from cnequity.storage.revisions import RevisionStore

    store = RevisionStore(config.meta_root, config.curated_root, config.derived_root, create=False)
    if store.current_root("adj_factors") != context.roots["adj_factors"]:
        return {"assessed": False, "reason": "legacy factors lack an immutable revision"}
    contradictions, payouts = factor_action_contradictions(config, symbols=symbols)
    latest = ReadContext.capture(config, ("adj_factors", "corporate_actions"), {})
    if dict(latest.roots) != dict(context.roots):
        return {"assessed": False, "reason": "inputs changed during diagnosis"}
    payload = {
        "schema_version": 1,
        "gate_impact": "none",
        "assessed": True,
        "factor_root": str(context.roots["adj_factors"]),
        "action_root": str(context.roots["corporate_actions"]),
        "scope": symbols,
        "issues": [{**row, "ex_date": str(row["ex_date"])} for row in contradictions.to_dicts()],
        "fund_cash_distributions_separate": payouts,
    }
    write_json_atomic(_path(config, context.roots["adj_factors"]), payload, indent=2)
    return payload


def factor_read_quality(
    config, bars: pl.DataFrame, *, factor_root: Path, factors: pl.DataFrame | None = None
) -> dict:
    """Only describe this factor revision and returned securities; never fetch."""
    result = {
        "gate_impact": "none",
        "historical_accuracy": "not_certified",
        "factor_coverage_is_not_accuracy": True,
        "diagnostics_assessed": False,
    }
    if "adj_is_exact" in bars.columns:
        result["missing_factor_rows"] = bars.filter(~pl.col("adj_is_exact")).height
    if factors is not None and "source" in factors.columns:
        if "adjust_type" in factors.columns:
            factors = factors.filter(pl.col("adjust_type") == "hfq")
        keys = bars.select("symbol", "trade_date").unique()
        counts = (
            factors.join(keys, on=["symbol", "trade_date"], how="semi")
            .drop_nulls("source")
            .group_by("source")
            .len()
            .sort("source")
        )
        result["factor_sources"] = dict(counts.iter_rows())
    path = _path(config, factor_root)
    if not path.exists() or bars.is_empty():
        return result
    try:
        snapshot = json.loads(path.read_text())
    except (OSError, ValueError):
        return result
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("issues"), list):
        return result
    if snapshot.get("scope") is not None and not isinstance(snapshot["scope"], list):
        return result
    try:
        for row in snapshot["issues"]:
            if not isinstance(row, dict) or not all(
                isinstance(row.get(key), str) for key in ("symbol", "kind", "ex_date")
            ):
                return result
            date.fromisoformat(row["ex_date"])
    except ValueError:
        return result
    if snapshot.get("factor_root") != str(factor_root):
        return result
    symbols = set(bars["symbol"].to_list())
    if snapshot.get("scope") is not None and not symbols.issubset(snapshot["scope"]):
        return result
    latest_days = dict(bars.group_by("symbol").agg(pl.col("trade_date").max()).iter_rows())
    issues = [
        row
        for row in snapshot.get("issues", [])
        if row.get("symbol") in symbols
        and row.get("ex_date", "9999") <= str(latest_days[row["symbol"]])
    ]
    result.update(
        diagnostics_assessed=True,
        related_event_count=len(issues),
        related_event_kinds=dict(Counter(row["kind"] for row in issues)),
        related_events=issues[:20],
        truncated=len(issues) > 20,
    )
    return result
