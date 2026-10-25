"""Build bounded, reproducible research panels from published lake generations."""
from __future__ import annotations

import bisect
import calendar as month_calendar
import hashlib
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from lake_equity.market.store import MarketStore
from .features import LEGACY_MA, PRICE_VOLUME, trailing_features


BAR_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
# Audited unadjusted OHLCV sources. ``ths`` was added after the 2026-10-24
# after-close audit (docs/provenance/ths_daily_bars_qualification.md): final
# 10-23 bars matched Sina field-for-field on 120/120 sampled symbols. Only
# after-close THS bars qualify; intraday THS rows must be refreshed, never used.
RAW_SOURCES = {"sina_unadjusted_v1", "eastmoney_unadjusted_v1", "tencent_star_unadjusted_v1", "fuyao_unadjusted_v1",
               "tushare", "tushare:daily", "baostock", "tdx_protocol", "eastmoney", "sina", "ths"}
MARGIN_COLUMNS = ["margin_balance_to_amount20", "margin_buy_to_amount", "margin_balance_change5",
                  "short_balance_to_margin", "short_sell_to_volume"]
FLOW_COLUMNS = ["flow_main_to_amount", "flow_large_to_amount", "flow_small_to_amount",
                "flow_net_to_amount_ths"]


def _check(check_cancel):
    if check_cancel is not None and check_cancel():
        from lake_equity.jobs import JobCancelled
        raise JobCancelled('Dataset build cancelled')


def _notify(progress, **payload):
    if progress is not None:
        progress(**payload)


def _sha(path, check_cancel):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            _check(check_cancel)
            value.update(block)
    return value.hexdigest()


def _overlaps(relative, start, end):
    for part in Path(relative).parts:
        if "=" not in part:
            continue
        value = part.split("=", 1)[1]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return start <= value <= end
        if re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = map(int, value.split("-"))
            return f"{value}-01" <= end and f"{value}-{month_calendar.monthrange(year, month)[1]:02d}" >= start
        if re.fullmatch(r"\d{4}", value):
            return f"{value}-01-01" <= end and f"{value}-12-31" >= start
    return True  # Unpartitioned published fixtures or tables: inspect their rows.


def _source_files(store, snapshot, dataset, start, end, check_cancel):
    item = snapshot["datasets"][dataset]
    generation = store._generation(dataset, item)
    files = []
    for record in item["receipt"]["generation_files"]:
        if not _overlaps(record["path"], start, end):
            continue
        path = generation.joinpath(*Path(record["path"]).parts[1:])
        digest = _sha(path, check_cancel)
        if digest != record["sha256"]:
            raise ValueError(f"Published source SHA256 changed: {dataset}: {path.name}")
        files.append({"path": str(path), "sha256": digest, "size_bytes": path.stat().st_size})
    return {"revision_id": item["revision_id"], "revision": item["revision"],
            "receipt_sha256": item["receipt_sha256"], "files": files}


class DatasetSession:
    """One pinned population read: verify once before batches and once at end."""
    def __init__(self, root, read_ref=None, check_cancel=None):
        self.store = MarketStore(root)
        self.snapshot = self.store.capture(read_ref)
        self.evidence = {}
        self.check_cancel = check_cancel

    def source_files(self, dataset, start, end):
        key = (dataset, start, end)
        if key not in self.evidence:
            self.evidence[key] = _source_files(self.store, self.snapshot, dataset, start, end, self.check_cancel)
        return self.evidence[key]

    def verify(self):
        for (dataset, _, _), evidence in self.evidence.items():
            self.store._generation(dataset, self.snapshot['datasets'][dataset])
            for item in evidence['files']:
                if _sha(Path(item['path']), self.check_cancel) != item['sha256']:
                    raise ValueError(f'Published source changed during dataset build: {dataset}')


def _read(store, snapshot, dataset, start, end, symbols, sources, check_cancel, session=None):
    _check(check_cancel)
    evidence = session.source_files(dataset, start, end) if session else _source_files(store, snapshot, dataset, start, end, check_cancel)
    sources[dataset] = evidence
    if not evidence["files"]:
        return pd.DataFrame()
    frame = pl.scan_parquet([item["path"] for item in evidence["files"]], hive_partitioning=False)
    schema = frame.collect_schema()
    if "trade_date" in schema:
        frame = frame.filter(pl.col("trade_date").is_between(date.fromisoformat(start), date.fromisoformat(end)))
    if symbols is not None:
        frame = frame.filter(pl.col("symbol").is_in(symbols))
    result = frame.collect().to_pandas()
    _check(check_cancel)
    return result


def _unique(frame, keys, dataset):
    if not frame.empty and frame.duplicated(keys).any():
        raise ValueError(f"Duplicate keys in {dataset}: {', '.join(keys)}")


def _valid_bars(bars):
    numbers = bars[BAR_COLUMNS].to_numpy(float)
    finite = np.isfinite(numbers).all(axis=1)
    positive = (numbers > 0).all(axis=1)
    geometry = (bars.high >= bars[["open", "close", "low"]].max(axis=1)) & (bars.low <= bars[["open", "close", "high"]].min(axis=1))
    return pd.Series(finite & positive, index=bars.index) & geometry & bars.source.isin(RAW_SOURCES)


def _labels(bars, valid, horizon):
    # The panel contains every calendar session, so shifts cannot jump missing days.
    label = bars.close.shift(-horizon) / bars.open.shift(-1) - 1
    end = pd.Series(bars.index, index=bars.index).shift(-horizon)
    mature = end.notna()
    all_valid = valid.copy()
    for offset in range(1, horizon + 1):
        all_valid &= valid.shift(-offset, fill_value=False)
    # Unadjusted splits/dividends are unverified. Quarantine large overnight
    # discontinuities, including the T -> T+1 transition before entry.
    jump = (bars.open / bars.close.shift(1) - 1).abs().gt(.15)
    abnormal = pd.Series(False, index=bars.index)
    for offset in range(1, horizon + 1):
        abnormal |= jump.shift(-offset, fill_value=False)
    reason = pd.Series("ok", index=bars.index)
    reason.loc[~all_valid] = "missing_invalid_or_unverified_bar"
    reason.loc[abnormal] = "unverified_corporate_action_or_large_gap"
    reason.loc[~mature] = "immature"
    return label.where(mature & all_valid & ~abnormal), end, reason


def _lag_funds(raw, calendar, dataset):
    if raw.empty:
        return raw
    _unique(raw, ["symbol", "trade_date"], dataset)
    raw = raw.copy()
    allowed = {"exchange", "eastmoney", "eastmoney_backfill"} if dataset == "margin_trading" else ({"eastmoney"} if dataset == "fund_flow" else {"ths"})
    if "source" not in raw:
        return raw.iloc[:0]
    raw = raw.loc[raw.source.isin(allowed)].copy()
    raw["observation_date"] = pd.to_datetime(raw.trade_date).dt.strftime("%Y-%m-%d")
    if dataset == "margin_trading":
        # SZ publishes a day later; no exact historical release timestamps exist.
        positions = [bisect.bisect_right(calendar, day) + 1 for day in raw.observation_date]
    else:
        if "fetched_at" not in raw:
            return raw.iloc[:0]
        observed = pd.to_datetime(raw.fetched_at, utc=True, errors="coerce").dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
        raw = raw.loc[observed.notna()].copy()
        effective = [max(day, seen) for day, seen in zip(raw.observation_date, observed.loc[raw.index])]
        positions = [bisect.bisect_right(calendar, day) for day in effective]
    raw["date"] = [calendar[i] if i < len(calendar) else None for i in positions]
    raw = raw.loc[raw.date.notna()]
    # Multiple snapshots may first become visible on one session. Keep the
    # latest actual observation, never backdate it to its market date.
    raw = raw.sort_values(["symbol", "date", "observation_date"])
    return raw.drop_duplicates(["symbol", "date"], keep="last")


def _fund_features(panel, raws, calendar):
    out = pd.DataFrame(index=panel.index)
    amount20 = panel.amount.rolling(20, min_periods=20).mean().replace(0, np.nan)
    observation = panel[["amount", "volume"]].copy()
    observation["amount20"] = amount20
    for dataset, raw in raws.items():
        if raw.empty:
            continue
        aligned = _lag_funds(raw, calendar, dataset)
        if aligned.empty:
            continue
        aligned = aligned.set_index("date")
        origin = observation.reindex(aligned.observation_date).reset_index(drop=True)
        origin.index = aligned.index
        def numeric(key):
            return pd.to_numeric(aligned.get(key, pd.Series(np.nan, index=aligned.index)), errors="coerce")
        if dataset == "margin_trading":
            values = pd.DataFrame(index=aligned.index)
            values["margin_balance_to_amount20"] = numeric("margin_balance") / origin.amount20
            values["margin_buy_to_amount"] = numeric("margin_buy") / origin.amount.replace(0, np.nan)
            values["short_balance_to_margin"] = numeric("short_balance") / numeric("margin_balance").replace(0, np.nan)
            values["short_sell_to_volume"] = numeric("short_sell_volume") / origin.volume.replace(0, np.nan)
            # Calendar alignment prevents a missing funding date from being
            # squeezed into a nominal five-session change.
            balance = numeric("margin_balance").reindex(panel.index)
            values = values.reindex(panel.index)
            values["margin_balance_change5"] = balance.pct_change(5, fill_method=None)
        elif dataset == "fund_flow":
            values = pd.DataFrame(index=aligned.index)
            for column, source in [("flow_main_to_amount", "main_net_inflow"),
                                   ("flow_large_to_amount", "large_net_inflow"),
                                   ("flow_small_to_amount", "small_net_inflow")]:
                values[column] = numeric(source) / origin.amount.replace(0, np.nan)
        else:
            values = pd.DataFrame({"flow_net_to_amount_ths": numeric("net_inflow") / numeric("amount").replace(0, np.nan)}, index=aligned.index)
        out = out.join(values.reindex(panel.index))
    return out.replace([np.inf, -np.inf], np.nan)


def _classification(snapshot, selected):
    result = {}
    for name, item in snapshot["datasets"].items():
        if name in selected:
            status, reason = "integrated", "Published generation read with file SHA256 verification"
        elif not item.get("available") or not item.get("rows"):
            status, reason = "unavailable", "No nonempty positive published generation"
        elif name in {"financial_statement_items", "share_structure", "shareholder_counts", "top_holders", "announcement_index"}:
            status, reason = "excluded", "PIT disclosure/observation and revision policy require a separate audited adapter"
        elif name in {"sector_members", "valuation_metrics", "analyst_consensus", "news_headlines", "hot_rank", "market_breadth", "trading_status", "etf_profiles"}:
            status, reason = "observation_only", "Current observations cannot be backdated into historical training"
        elif name in {"industry_members", "industry_index", "index_constituents", "instruments", "delisting_events"}:
            status, reason = "excluded", "Historical population/classification or original publication visibility remains unverified"
        elif name in {"adj_factors", "corporate_actions"}:
            status, reason = "excluded", "Adjustment and corporate-action reconciliation is pending; raw-price research only"
        else:
            status, reason = "excluded", "No audited feature/availability adapter in this implementation"
        result[name] = {"status": status, "reason": reason, "published_rows": int(item.get("rows", 0)), "revision_id": item.get("revision_id")}
    return result


def build_dataset(root: Path, spec: dict, *, progress=None, check_cancel=None, session=None) -> dict:
    symbols = spec.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 500 or any(not isinstance(s, str) for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("symbols must contain 1..500 unique explicit Shanghai/Shenzhen A-share identities")
    if any(not isinstance(s, str) or not re.fullmatch(r"(?:6\d{5}\.SH|[03]\d{5}\.SZ)", s) for s in symbols):
        raise ValueError("Only explicit Shanghai/Shenzhen A-share symbols are supported")
    start, end = spec.get("start"), spec.get("end")
    if not isinstance(start, str) or not isinstance(end, str) or date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or start > end:
        raise ValueError("start/end must be ordered ISO dates")
    horizon = spec.get("horizon", 5)
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 20:
        raise ValueError("horizon must be an integer between 1 and 20 sessions")
    requested = spec.get("factor_groups", ["price_volume", "margin"])
    if not isinstance(requested, list) or not requested or any(g not in {"price_volume", "legacy_ma", "margin", "money_flow"} for g in requested):
        raise ValueError("Unknown or empty factor_groups")
    _check(check_cancel)
    store = session.store if session else MarketStore(root)
    _notify(progress, phase="dataset_capture", message="Pinning published lake generations")
    snapshot = session.snapshot if session else store.capture(spec.get("read_ref"))
    if spec.get('read_ref') and spec['read_ref'] != snapshot['read_ref']:
        raise ValueError('Dataset batch differs from its pinned session')
    sources = {}
    for required in ("trading_calendar", "daily_bars"):
        if not snapshot["datasets"][required].get("available"):
            raise ValueError(f"Published {required} is required")
    calendar_frame = _read(store, snapshot, "trading_calendar", "1900-01-01", end, None, sources, check_cancel, session)
    _unique(calendar_frame, ["trade_date"], "trading_calendar")
    if calendar_frame.empty or "is_trading" not in calendar_frame:
        raise ValueError("Published trading calendar is empty or invalid")
    calendar_frame = calendar_frame.loc[calendar_frame.is_trading.eq(True)]
    calendar = sorted(pd.to_datetime(calendar_frame.trade_date).dt.strftime("%Y-%m-%d").tolist())
    first = bisect.bisect_left(calendar, start)
    if first == len(calendar):
        raise ValueError("No verified trading sessions in the requested window")
    warmup_start = calendar[max(0, first - 180)]
    sessions = calendar[max(0, first - 180):]
    if len(sessions) * len(symbols) > 1_750_000:
        raise ValueError("Requested panel exceeds 1,750,000 rows; narrow dates or explicit symbols")
    bars = _read(store, snapshot, "daily_bars", warmup_start, end, symbols, sources, check_cancel, session)
    if bars.empty:
        bars = pd.DataFrame(columns=['symbol', 'trade_date', 'source', *BAR_COLUMNS])
    if not {"symbol", "trade_date", "source", *BAR_COLUMNS} <= set(bars):
        raise ValueError("Published bars are missing required OHLCV/amount/source columns")
    _unique(bars, ["symbol", "trade_date"], "daily_bars")
    bars["date"] = pd.to_datetime(bars.trade_date).dt.strftime("%Y-%m-%d")
    if not set(bars.date) <= set(calendar):
        raise ValueError("Published bars contain dates absent from the verified trading calendar")
    for column in BAR_COLUMNS:
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    raw_funds = {}
    requested_datasets = (["margin_trading"] if "margin" in requested else []) + (["fund_flow", "fund_flow_ths"] if "money_flow" in requested else [])
    for dataset in requested_datasets:
        if snapshot["datasets"][dataset].get("available"):
            raw_funds[dataset] = _read(store, snapshot, dataset, warmup_start, end, symbols, sources, check_cancel, session)
    groups = {"legacy_ma": list(LEGACY_MA), "price_volume": list(PRICE_VOLUME)}
    if "margin" in requested:
        groups["margin"] = list(MARGIN_COLUMNS)
    if "money_flow" in requested:
        groups["money_flow"] = list(FLOW_COLUMNS)
    output = []
    for i, symbol in enumerate(symbols):
        _check(check_cancel)
        panel = bars.loc[bars.symbol.eq(symbol)].set_index("date").reindex(sessions)
        panel.index.name = "date"
        panel["symbol"] = symbol
        valid = _valid_bars(panel)
        masked = panel[BAR_COLUMNS].where(valid, np.nan)
        factors = trailing_features(masked)
        funds = _fund_features(masked, {d: f.loc[f.symbol.eq(symbol)] if not f.empty else f for d, f in raw_funds.items()}, calendar)
        factors = factors.join(funds)
        for columns in groups.values():
            for column in columns:
                if column not in factors:
                    factors[column] = np.nan
        panel = panel.join(factors)
        panel["bar_valid"] = valid
        panel["feature_eligible"] = valid.rolling(65, min_periods=65).sum().eq(65)
        panel["legacy_ma_eligible"] = valid.rolling(120, min_periods=120).sum().eq(120)
        panel["label"], panel["label_end"], panel["label_reason"] = _labels(panel, valid, horizon)
        panel["label_end"] = panel.label_end.where(panel.label_end.notna(), None)
        panel["feature_available_at"] = panel.index + "T15:00:00+08:00"
        output.append(panel.loc[panel.index >= start].reset_index())
        _notify(progress, phase="dataset_features", current=i + 1, total=len(symbols), symbol=symbol)
    frame = pd.concat(output, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
    # Verify actual bytes again after all numerical reads, not just metadata sizes.
    for dataset, evidence in (sources.items() if session is None else []):
        store._generation(dataset, snapshot["datasets"][dataset])
        for item in evidence["files"]:
            _check(check_cancel)
            if _sha(Path(item["path"]), check_cancel) != item["sha256"]:
                raise ValueError(f"Published source changed during dataset build: {dataset}")
    coverage = {}
    for name, columns in groups.items():
        finite = np.isfinite(frame[columns].to_numpy(float))
        available = int(finite.any(axis=1).sum())
        coverage[name] = {"columns": columns, "nonempty_rows": available,
                          "complete_rows": int(finite.all(axis=1).sum()), "nonempty_values": int(finite.sum()),
                          "row_coverage": available / len(frame) if len(frame) else 0.0,
                          "reason": "observed_nonempty" if available else "no_published_available_values_in_window",
                          "source_datasets": ["daily_bars"] if name in {"price_volume", "legacy_ma"} else (["margin_trading"] if name == "margin" else ["fund_flow", "fund_flow_ths"])}
    report = {"rows": len(frame), "symbols_requested": symbols, "symbols_with_bars": int(bars.symbol.nunique()),
              "start": start, "end": end, "warmup_start": warmup_start, "horizon": horizon,
              "label_formula": "close[T+horizon]/open[T+1]-1; every verified calendar session required",
              "label_reason_counts": {str(k): int(v) for k, v in frame.label_reason.value_counts().items()},
              "valid_bar_rows": int(frame.bar_valid.sum()), "feature_eligible_rows": int(frame.feature_eligible.sum()),
              "group_coverage": coverage, "dataset_classification": _classification(snapshot, sources),
              "strict_history_eligible": False, "historical_population_complete": False,
              "atomic_across_datasets": False, "adjustment_status": "unadjusted_research_only",
              "availability_policies": {"margin": "trade_date_plus_2_sessions_assumption_not_verified_PIT",
                                        "money_flow": "next_session_after_max_trade_date_and_actual_fetched_local_date_no_forward_fill"},
              "limitations": ["Explicit sample is not a complete historical population; missing delisted stocks remain unknown.",
                              "Corporate actions/adjustments are unverified. Labels crossing >15% overnight gaps are excluded; smaller actions remain a raw-price limitation.",
                              "Legacy MA formulas are reproduced; this does not grant eligibility under the old model's provenance contract.",
                              "Funding publication lags are disclosed research assumptions, not certified historical PIT."]}
    return {"frame": frame, "calendar": calendar, "feature_groups": groups, "report": report,
            "sources": {"read_ref": snapshot["read_ref"], "captured_at": snapshot["captured_at"], "datasets": sources}}
