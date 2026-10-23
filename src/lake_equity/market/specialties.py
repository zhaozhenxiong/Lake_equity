from __future__ import annotations

import json
import math
from datetime import date, datetime

import polars as pl

from lake_equity.market.store import MarketStore


def _json(value):
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _base(snapshot: dict, dataset: str, title: str, panel_id: str | None = None) -> dict:
    item = snapshot["datasets"][dataset]
    units = dict(item["units"]) if isinstance(item["units"], dict) else {"contract": item["units"]}
    return {"id": panel_id or dataset, "title": title, "dataset": dataset, "status": "available" if item["available"] else "unavailable", "available": bool(item["available"]), "columns": [], "rows": [], "summary": {"revision_id": item["revision_id"], "coverage_start": item["coverage_start"], "coverage_end": item["coverage_end"]}, "units": units, "notes": ["仅展示冻结发布代次的观测；覆盖存在不等于历史完整或严格 PIT 合格。"], "total_count": 0, "truncated": False}


def _unavailable(panel: dict, item: dict) -> dict:
    panel["notes"].append(item.get("reason", "No published generation"))
    return panel


def _latest(store: MarketStore, snapshot: dict, dataset: str) -> pl.DataFrame:
    frame = store.scan(dataset, snapshot)
    item = snapshot["datasets"][dataset]
    column = item["date_column"]
    if column and item["coverage_end"] is not None:
        frame = frame.filter(pl.col(column).cast(pl.String) == item["coverage_end"])
    return frame.collect()


def _table(panel: dict, frame: pl.DataFrame, limit: int = 100, columns: list[str] | None = None) -> dict:
    selected = [column for column in (columns or frame.columns) if column in frame.columns]
    panel.update(columns=[{"key": column, "label": column, "unit": panel["units"].get(column)} for column in selected], rows=_json(frame.select(selected).head(limit).to_dicts()), total_count=frame.height, truncated=frame.height > limit)
    if frame.is_empty():
        panel["status"] = "empty"
    return panel


def _counts(frame: pl.DataFrame, column: str) -> dict:
    if column not in frame.columns:
        return {}
    return {str(row[column]): row["count"] for row in frame.group_by(column).agg(pl.len().alias("count")).sort(column, nulls_last=True).to_dicts()}


def _breadth(store: MarketStore, snapshot: dict) -> list[dict]:
    dataset = "market_breadth"
    panel = _base(snapshot, dataset, "市场广度")
    if not panel["available"]:
        return [_unavailable(panel, snapshot["datasets"][dataset])]
    frame = _latest(store, snapshot, dataset).sort("metric_id")
    panel["summary"].update(as_of=snapshot["datasets"][dataset]["coverage_end"], metrics={row["metric_id"]: row["value"] for row in frame.to_dicts()}, observed_metric_count=frame.height)
    panel["units"].update(advance_ratio="fraction", advance_count="count", decline_count="count", flat_count="count", limit_up_count="count", limit_down_count="count", total_count="count")
    panel["notes"].append("涨跌停数来自行情涨幅阈值启发式，非交易所精确涨跌停清单；没有的交易日不补零。")
    return [_table(panel, frame, columns=["metric_id", "value", "trade_date", "source"])]


def _valuation(store: MarketStore, snapshot: dict) -> list[dict]:
    dataset = "valuation_metrics"
    panel = _base(snapshot, dataset, "全观测横截面估值分布")
    if not panel["available"]:
        return [_unavailable(panel, snapshot["datasets"][dataset])]
    frame = _latest(store, snapshot, dataset)
    distributions = []
    metrics = {}
    for metric, boundaries in (("pe_ttm", [10, 20, 40, 80]), ("pb", [1, 2, 4, 8])):
        values = frame[metric]
        finite = values.filter(values.is_not_null() & values.is_finite())
        positive = finite.filter(finite > 0)
        metrics[metric] = {"observed_count": len(values), "finite_count": len(finite), "missing_count": values.null_count(), "invalid_count": len(values) - values.null_count() - len(finite), "nonpositive_count": len(finite.filter(finite <= 0)), "positive_count": len(positive), "positive_median": positive.median(), "positive_p25": positive.quantile(0.25), "positive_p75": positive.quantile(0.75)}
        distributions.extend([{"metric": metric, "bucket": "missing", "count": values.null_count()}, {"metric": metric, "bucket": "nonfinite", "count": metrics[metric]["invalid_count"]}, {"metric": metric, "bucket": "<=0", "count": metrics[metric]["nonpositive_count"]}])
        lower = 0
        for upper in boundaries:
            distributions.append({"metric": metric, "bucket": f"({lower},{upper}]", "count": len(positive.filter((positive > lower) & (positive <= upper)))})
            lower = upper
        distributions.append({"metric": metric, "bucket": f">{lower}", "count": len(positive.filter(positive > lower))})
    panel["summary"].update(as_of=snapshot["datasets"][dataset]["coverage_end"], population_count=frame.height, unique_symbols=frame["symbol"].n_unique(), metrics=metrics, source_counts=_counts(frame, "source"))
    panel["notes"].append("分布先使用最新日期全部观测行计算，再展示；不由前100条样本推算。PE 使用 TTM，不与动态 PE 混用；非正值、缺失值分开。没有历史时不宣称历史分位。")
    panel["units"].update(count="count", bucket="ratio")
    _table(panel, pl.DataFrame(distributions))
    preview = _base(snapshot, dataset, "估值原始明细预览", "valuation_preview")
    preview["summary"].update(as_of=snapshot["datasets"][dataset]["coverage_end"], population_count=frame.height, total_mv_basis_counts=_counts(frame, "total_mv_basis"), float_mv_basis_counts=_counts(frame, "float_mv_basis"))
    _table(preview, frame.sort("symbol"), columns=["symbol", "trade_date", "pe_ttm", "pe_dynamic", "pb", "ps_ttm", "total_mv", "float_mv", "total_mv_basis", "float_mv_basis", "shares_as_of", "source"])
    return [panel, preview]


def _industry(store: MarketStore, snapshot: dict) -> list[dict]:
    dataset = "industry_index"
    empty = _base(snapshot, dataset, "申万行业量价轮动")
    if not empty["available"]:
        return [_unavailable(empty, snapshot["datasets"][dataset])]
    frame = _latest(store, snapshot, dataset)
    panels = []
    for key, group in frame.partition_by(["level", "weighting"], as_dict=True).items():
        level, weighting = key
        panel = _base(snapshot, dataset, f"申万 {level} · {weighting}", f"industry_{level}_{weighting}")
        finite = group.filter(pl.col("ret").is_finite())
        panel["summary"].update(as_of=snapshot["datasets"][dataset]["coverage_end"], level=level, weighting=weighting, industry_count=group.height, priced_industries=finite.height, positive_count=finite.filter(pl.col("ret") > 0).height, negative_count=finite.filter(pl.col("ret") < 0).height, median_ret=finite["ret"].median(), n_members=group["n_members"].sum(), n_priced=group["n_priced"].sum(), n_excluded=group["n_excluded"].sum())
        panel["notes"].extend(["湖内派生行业收益，非官方申万指数价格；ret 为小数收益率。", "层级和权重分别计算，不跨层级/权重汇总成交额，不按名称拼接同花顺或东方财富成员。", "成员采用当日可对应的历史月度快照；缺少原始当时可知版本证明时，不升级为严格 PIT。"])
        panels.append(_table(panel, group.sort("ret", descending=True, nulls_last=True), limit=120, columns=["industry_code", "level", "weighting", "ret", "n_members", "n_priced", "n_excluded", "amount", "trade_date", "source"]))
    return panels or [_table(empty, frame)]


def _flows(store: MarketStore, snapshot: dict) -> list[dict]:
    definitions = [("fund_flow", "东方财富个股主力净流入"), ("sector_fund_flow", "东方财富板块主力净流入"), ("fund_flow_ths", "同花顺个股资金流"), ("sector_fund_flow_ths", "同花顺板块资金流"), ("margin_trading", "融资融券"), ("northbound_flows", "北向资金历史"), ("northbound_holdings", "北向持股历史")]
    panels = []
    for dataset, title in definitions:
        panel = _base(snapshot, dataset, title)
        if not panel["available"]:
            panels.append(_unavailable(panel, snapshot["datasets"][dataset]))
            continue
        frame = _latest(store, snapshot, dataset)
        panel["summary"].update(as_of=snapshot["datasets"][dataset]["coverage_end"], population_count=frame.height, source_counts=_counts(frame, "source"))
        if "sector" in dataset:
            panel["notes"].append("板块存在成份重叠，不把各板块净流入加总为市场净流入。")
        if dataset.startswith("northbound"):
            panel["status"] = "historical"
            panel["notes"].append("北向公开披露口径自2024年8月发生变化；仅展示实际历史日期，不作为当日实时资金。")
        panel["notes"].append("东方财富主力净流与同花顺总流入/流出不是同一指标，分别展示；成交额增减绝不是资金净流入。")
        order = next((column for column in ("main_net_inflow", "net_inflow", "net_buy", "margin_balance", "holding_mv") if column in frame.columns), None)
        if order:
            frame = frame.sort(order, descending=True, nulls_last=True)
        panels.append(_table(panel, frame))
    return panels


def _derivatives(store: MarketStore, snapshot: dict) -> list[dict]:
    definitions = [("commodity_bars", "商品行情"), ("futures_bars", "期货合约行情"), ("option_bars", "期权合约行情"), ("futures_continuous", "期货连续序列"), ("option_greeks", "期权 IV / Greeks")]
    panels = []
    for dataset, title in definitions:
        panel = _base(snapshot, dataset, title)
        if not panel["available"]:
            panels.append(_unavailable(panel, snapshot["datasets"][dataset]))
            continue
        frame = _latest(store, snapshot, dataset)
        panel["summary"].update(as_of=snapshot["datasets"][dataset]["coverage_end"], population_count=frame.height, symbol_count=frame["symbol"].n_unique(), exchange_counts=_counts(frame, "exchange"), source_counts=_counts(frame, "source"))
        if "volume" in frame.columns:
            panel["summary"]["zero_volume_count"] = frame.filter(pl.col("volume") == 0).height
        if "status" in frame.columns:
            panel["summary"]["model_status_counts"] = _counts(frame, "status")
            panel["notes"].append("IV/Greeks 为模型派生，必须保留 model/status/rate_source；无解或无基础标的不补零。")
        panel["notes"].append("不同品种报价/合约乘数不同，不跨品种汇总价格或原生金额；无成交但有结算价的合约不伪造 OHLC。")
        order = "volume" if "volume" in frame.columns else "symbol"
        panels.append(_table(panel, frame.sort(order, descending=order == "volume", nulls_last=True)))
    return panels


def _macro(store: MarketStore, snapshot: dict) -> list[dict]:
    dataset = "macro_indicators"
    panel = _base(snapshot, dataset, "宏观指标最近观测")
    if not panel["available"]:
        return [_unavailable(panel, snapshot["datasets"][dataset])]
    frame = store.scan(dataset, snapshot).sort(["indicator_id", "obs_date", "fetched_at"]).group_by("indicator_id", maintain_order=True).tail(1).collect().sort("indicator_id")
    panel["summary"].update(indicator_count=frame.height, source_counts=_counts(frame, "source"))
    panel["notes"].append("每个指标各自取最新观测日期；obs_date 不是发布日期，不将后来修订值宣称为历史当时已知值。各指标 source_native 单位不混加。")
    return [_table(panel, frame, columns=["indicator_id", "obs_date", "value", "frequency", "source", "fetched_at"])]


def _intraday(store: MarketStore, snapshot: dict) -> list[dict]:
    panels = []
    watchlist = list(store.config.minute_bars_symbols)
    for dataset, title in (("index_bars", "指数日行情"), ("minute_bars", "关注池 1 分钟"), ("minute_bars_5m", "关注池 5 分钟")):
        panel = _base(snapshot, dataset, title)
        panel["summary"].update(configured_scope=store.config.minute_bars_scope if dataset != "index_bars" else "published_indices", configured_watchlist=watchlist if dataset != "index_bars" else [])
        if not panel["available"]:
            panel["summary"]["watchlist_status"] = [{"symbol": symbol, "available": False, "rows": 0} for symbol in watchlist] if dataset != "index_bars" else []
            panels.append(_unavailable(panel, snapshot["datasets"][dataset]))
            continue
        frame = _latest(store, snapshot, dataset)
        if dataset != "index_bars" and watchlist:
            counts = dict(frame.group_by("symbol").len().iter_rows())
            panel["summary"]["watchlist_status"] = [{"symbol": symbol, "available": counts.get(symbol, 0) > 0, "rows": counts.get(symbol, 0)} for symbol in watchlist]
            frame = frame.filter(pl.col("symbol").is_in(watchlist))
        panel["summary"].update(as_of=snapshot["datasets"][dataset]["coverage_end"], population_count=frame.height, source_counts=_counts(frame, "source"))
        panel["notes"].append("只展示已发布真实行情；关注池覆盖不是全市场覆盖，缺失不会用零价格或日线插值填充。")
        order = [column for column in ("symbol", "bar_time", "trade_date") if column in frame.columns]
        panels.append(_table(panel, frame.sort(order), limit=100))
    return panels


def specialty_view(store: MarketStore, snapshot: dict, section: str) -> dict:
    sections = {"breadth": _breadth, "valuation": _valuation, "flows": _flows, "industry": _industry, "derivatives": _derivatives, "macro": _macro, "intraday": _intraday}
    if section not in sections:
        raise ValueError(f"Unknown market specialty section: {section}")
    retained = store._manifest(snapshot.get("read_ref"))
    if retained != snapshot:
        raise ValueError("Market specialty snapshot differs from its retained manifest")
    payload = {"read_ref": snapshot["read_ref"], "section": section, "panels": sections[section](store, snapshot), "atomic_across_datasets": False, "historical_completeness": "not_assessed"}
    return json.loads(json.dumps(_json(payload), ensure_ascii=False, allow_nan=False))
