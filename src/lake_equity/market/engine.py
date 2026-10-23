from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
from cnequity.domain import symbols as symbol_contract
from filelock import FileLock

from .taxonomy import recover_taxonomy


FEATURE_COLUMNS = ("board_return_1", "board_return_5", "board_return_10", "board_return_20",
    "board_advance_ratio", "board_above_ma20_ratio", "board_breakout20_ratio", "board_amount",
    "board_market_amount_share", "board_relative_amount20", "board_volatility20", "board_drawdown20")
POLICY = {"min_members": 5, "min_coverage": .8, "sessions_per_year": 252}
WARNINGS = ["成交额是成交活动代理，不是资金净流入。", "板块成员存在重叠，板块成交额不可相加作为全市场资金。",
    "固定当前成员回看历史，仅供观察；不具备历史成员PIT或训练资格。",
    "市场分母为本地已观测沪深A股有效行情，不代表交易所全人口总额。",
    "未复权成员收益为等权观察摘要，不是官方板块指数或可执行组合收益。",
    "活跃度同时考虑绝对涨跌幅，活跃不等于强势。"]


def _bytes(value) -> bytes:
    def encode(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(f"Unsupported market JSON type: {type(item).__name__}")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=encode).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _number(value):
    return float(value) if value is not None and math.isfinite(float(value)) else None


def _supported(symbol) -> bool:
    if not isinstance(symbol, str) or re.fullmatch(r"\d{6}\.(?:SH|SZ)", symbol) is None:
        return False
    code, exchange = symbol.split(".")
    return symbol_contract.is_all_a_symbol(code, exchange) and not symbol_contract.is_cdr_symbol(code, exchange)


def _frame_hash(frame: pl.DataFrame, columns: list[str]) -> str:
    return hashlib.sha256(frame.sort(columns).write_csv().encode("utf-8")).hexdigest()


def _activity(boards: list[dict]) -> None:
    for kind in ("industry", "concept", "mixed"):
        rows = [row for row in boards if row["kind"] == kind and row["relative_amount20"] is not None and row["return_1"] is not None]
        ranks = {}
        for field in ("relative_amount20", "return_1"):
            ordered = sorted((abs(row[field]) if field == "return_1" else row[field], row["board_id"]) for row in rows)
            start = 0
            while start < len(ordered):
                stop = start + 1
                while stop < len(ordered) and ordered[stop][0] == ordered[start][0]:
                    stop += 1
                percentile = .5 if len(ordered) == 1 else ((start + stop - 1) / 2) / (len(ordered) - 1)
                for _, board_id in ordered[start:stop]:
                    ranks[(board_id, field)] = percentile
                start = stop
        for row in rows:
            row["activity_score"] = 50 * (ranks[(row["board_id"], "relative_amount20")] + ranks[(row["board_id"], "return_1")])


def _window(values, count: int, previous: bool = False):
    selected = values[-count - 1:-1] if previous else values[-count:]
    return selected if len(selected) == count and np.isfinite(selected).all() else None


def _features(returns, amounts, advance, above, breakout, market_share) -> dict:
    result = {}
    for count in (1, 5, 10, 20):
        values = _window(returns, count)
        result[f"board_return_{count}"] = _number(np.prod(1 + values) - 1) if values is not None else None
    for field, values in (("board_advance_ratio", advance), ("board_above_ma20_ratio", above),
            ("board_breakout20_ratio", breakout), ("board_amount", amounts), ("board_market_amount_share", market_share)):
        result[field] = _number(values[-1])
    prior = _window(amounts, 20, previous=True)
    mean = float(prior.mean()) if prior is not None else None
    result["board_relative_amount20"] = _number(amounts[-1] / mean) if mean is not None and mean > 0 else None
    values = _window(returns, 20)
    result["board_volatility20"] = _number(np.std(values, ddof=1) * math.sqrt(252)) if values is not None else None
    if values is None:
        result["board_drawdown20"] = None
    else:
        nav = np.r_[1., np.cumprod(1 + values)]
        result["board_drawdown20"] = _number(np.max(1 - nav / np.maximum.accumulate(nav)))
    return result


class MarketFeaturesService:
    def __init__(self, store):
        self.store = store
        self.root = Path(store.root) / "var" / "market_views"

    def _path(self, *parts) -> Path:
        path = self.root.joinpath(*parts)
        for candidate in (self.root, *path.parents, path):
            if candidate.is_symlink():
                raise ValueError("Market view paths cannot contain symlinks")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Unsafe market view path")
        return path

    def _read(self, view_id: str) -> dict:
        if not isinstance(view_id, str) or re.fullmatch(r"[0-9a-f]{64}", view_id) is None:
            raise ValueError("Invalid market view_id")
        payload = json.loads(self._path("views", view_id, "view.json").read_bytes())
        content = {key: value for key, value in payload.items() if key != "payload_sha256"}
        if payload.get("view_id") != view_id or _digest(payload.get("identity")) != view_id or _digest(content) != payload.get("payload_sha256"):
            raise ValueError("Market view payload identity mismatch")
        return payload

    def status(self) -> dict | None:
        pointer = self._path("latest.json")
        if not pointer.exists():
            return None
        return self._read(json.loads(pointer.read_bytes())["view_id"])

    def prepare(self, end=None, basis="members") -> dict:
        if basis not in {"members", "source"}:
            raise ValueError("Unknown market feature basis")
        end = date.fromisoformat(str(end)).isoformat() if end is not None else None
        self._path().mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._path("prepare.lock")), timeout=0):
            snapshot = self.store.capture()
            dataset = "daily_bars" if basis == "members" else "sector_bars"
            item = snapshot["datasets"].get(dataset, {})
            if not item.get("available"):
                raise ValueError(f"No published {dataset} generation")
            if basis == "source" and item.get("units", {}).get("amount") != "source_native":
                raise ValueError("Native board turnover requires its explicit source_native amount contract")
            calendar = self.store.scan("trading_calendar", snapshot).filter(pl.col("is_trading") == True).select("trade_date").unique().sort("trade_date").collect()
            sessions = [str(value) for value in calendar["trade_date"].to_list()]
            latest = self.store.scan(dataset, snapshot).select(pl.col("trade_date").max()).collect().item()
            cutoff = end or str(latest)
            dates = [value for value in sessions if value <= cutoff][-81:]
            if not dates:
                raise ValueError("No published calendar sessions before requested end")
            prices = self.store.scan(dataset, snapshot).filter(pl.col("trade_date").is_between(date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]))).collect()
            if prices.is_empty():
                raise ValueError("No observed prices in requested calendar window")
            if basis == "members":
                boards, details, metadata = self._members_prepare(snapshot, prices, dates)
            else:
                boards, details, metadata = self._source_prepare(prices, dates)
            _activity(boards)
            code = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (Path(__file__), Path(__file__).with_name("taxonomy.py"), Path(symbol_contract.__file__))}
            identity = {"schema_version": 1, "algorithm": "lake_board_observation_v1", "code_sha256": code,
                "read_ref": snapshot["read_ref"], "basis": basis, "sessions": dates, "policy": POLICY,
                "price_content_sha256": _frame_hash(prices, ["trade_date", "symbol"] if basis == "members" else ["source", "sector_code", "trade_date"]),
                **metadata["identity"]}
            view_id = _digest(identity)
            view = {"view_id": view_id, "read_ref": snapshot["read_ref"], "basis": basis, "identity": identity,
                "created_at": datetime.now(timezone.utc).isoformat(), "price_date": dates[-1], "membership_date": metadata.get("membership_date"),
                "units": {"amount": "CNY" if basis == "members" else "source_native", "ratio": "fraction", "return": "fraction"},
                "summary": {"market_amount": metadata.get("market_amount"), "available_boards": sum(row["amount"] is not None for row in boards),
                    "total_boards": len(boards), "observed_market_members": metadata.get("observed_market_members"),
                    "historical_completeness": "not_assessed"}, "boards": boards,
                "top_gainers": self._top(boards, positive=True), "top_losers": self._top(boards, positive=False),
                "warnings": list(WARNINGS if basis == "members" else WARNINGS[:2] + WARNINGS[-1:]) + metadata.get("warnings", []), "training_eligible": False,
                "qualification": "observation_only", "mode": "current_reference", "pit_eligible": False,
                "capital_basis": "turnover_amount_proxy" if basis == "members" else "provider_board_turnover_source_native",
                "membership_basis": "fixed_current_snapshot_historical_price_comparison" if basis == "members" else "unsupported",
                "overlapping_boards": True, "detail_sha256": {key: _digest(value) for key, value in details.items()}}
            destination = self._path("views", view_id)
            if destination.exists():
                view = self._read(view_id)
            else:
                temporary = self._path("views", f".prepare-{uuid.uuid4().hex}")
                temporary.mkdir(parents=True)
                try:
                    for board_id, detail in details.items():
                        (temporary / f"{_digest(board_id)}.json").write_bytes(_bytes(detail))
                    view["payload_sha256"] = _digest(view)
                    (temporary / "view.json").write_bytes(_bytes(view))
                    os.replace(temporary, destination)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            pointer = self._path(f".latest-{uuid.uuid4().hex}.json")
            pointer.write_bytes(_bytes({"view_id": view_id}))
            os.replace(pointer, self._path("latest.json"))
            return view

    @staticmethod
    def _top(rows, *, positive):
        population = [row for row in rows if row["amount_change"] is not None and (row["amount_change"] > 0 if positive else row["amount_change"] < 0)]
        return sorted(population, key=lambda row: (-row["amount_change"] if positive else row["amount_change"], row["board_id"]))[:5]

    def capital_view(self, view_id, kind="all", sort_by="amount", limit=30):
        if kind not in {"all", "industry", "concept", "mixed"} or sort_by not in {"amount", "amount_change", "relative_amount", "activity"}:
            raise ValueError("Unknown market feature filter")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Market feature limit must be in 1..100")
        view = self._read(view_id)
        rows = [row for row in view["boards"] if kind == "all" or row["kind"] == kind]
        field = {"amount_change": "amount_change", "amount": "amount", "relative_amount": "relative_amount20", "activity": "activity_score"}[sort_by]
        ordered = sorted(rows, key=lambda row: (row[field] is None, -abs(row[field]) if field == "amount_change" and row[field] is not None else -row[field] if row[field] is not None else 0, row["board_id"]))
        return {**view, "boards": ordered[:limit], "top_gainers": self._top(rows, positive=True),
                "top_losers": self._top(rows, positive=False), "filtered_boards": len(rows), "kind": kind, "sort_by": sort_by, "limit": limit}

    def _detail(self, view_id, board_id):
        view = self._read(view_id)
        expected = view["detail_sha256"].get(board_id)
        if expected is None:
            raise FileNotFoundError("Board is not present in pinned market view")
        detail = json.loads(self._path("views", view_id, f"{_digest(board_id)}.json").read_bytes())
        if _digest(detail) != expected:
            raise ValueError("Market board detail identity mismatch")
        return view, detail

    def history(self, view_id, board_id, window=20):
        if window not in (20, 60):
            raise ValueError("Market history window must be 20 or 60")
        view, detail = self._detail(view_id, board_id)
        return {"view_id": view_id, "read_ref": view["read_ref"], "board_id": board_id, "basis": view["basis"],
            "window": window, "points": detail["history"][-window:], "units": view["units"],
            "membership_date": view["membership_date"], "warnings": view["warnings"], "training_eligible": False}

    def members(self, view_id, board_id, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid member pagination")
        view, detail = self._detail(view_id, board_id)
        return {"view_id": view_id, "read_ref": view["read_ref"], "board_id": board_id, "basis": view["basis"],
            "offset": offset, "limit": limit, "total_count": len(detail["members"]), "rows": detail["members"][offset:offset + limit],
            "status": "unsupported" if detail["mapping_status"] == "unsupported" else "available",
            "mapping_status": detail["mapping_status"], "membership_date": view["membership_date"],
            "warnings": view["warnings"], "training_eligible": False}

    def _members_prepare(self, snapshot, prices, dates):
        unit = snapshot["datasets"]["daily_bars"].get("units", {}).get("amount")
        if unit != "CNY":
            raise ValueError("Member turnover requires an explicit CNY amount contract")
        membership = self.store.scan("sector_members", snapshot)
        membership_date = membership.select(pl.col("as_of_date").max()).collect().item()
        member_frame = membership.filter(pl.col("as_of_date") == membership_date).collect()
        member_rows = member_frame.to_dicts()
        taxonomy = recover_taxonomy(self.store, snapshot, member_rows)
        frame = pd.DataFrame(prices.to_dicts())
        frame = frame.loc[frame.symbol.map(_supported)].copy()
        frame["trade_date"] = frame.trade_date.astype(str)
        numeric = ["open", "high", "low", "close", "volume", "amount"]
        frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
        values = frame[numeric]
        valid = (~frame.duplicated(["symbol", "trade_date"], keep=False) & np.isfinite(values).all(axis=1)
            & values[["open", "high", "low", "close"]].gt(0).all(axis=1) & values[["volume", "amount"]].ge(0).all(axis=1)
            & values.high.ge(values[["open", "close", "low"]].max(axis=1))
            & values.low.le(values[["open", "close", "high"]].min(axis=1))
            & frame.source.notna() & ~frame.source.isin(["", "mock"]))
        if "has_trade_price" in frame:
            valid &= frame.has_trade_price.eq(1)
        if "available_at" in frame:
            known = pd.to_datetime(frame.available_at, errors="coerce", utc=True)
            valid &= known.notna() & known.le(pd.to_datetime(frame.trade_date + "T15:00:00+08:00", utc=True))
        frame.loc[~valid, numeric] = np.nan
        frame = frame.drop_duplicates(["symbol", "trade_date"])
        symbols = sorted(set(frame.symbol) | {row["symbol"] for row in member_rows if _supported(row["symbol"])})
        matrices = {field: frame.pivot(index="trade_date", columns="symbol", values=field).reindex(index=dates, columns=symbols)
            for field in ("close", "high", "amount")}
        close = matrices["close"]
        returns = close / close.shift(1) - 1
        ma = close.rolling(20, min_periods=20).mean()
        high = matrices["high"].shift(1).rolling(20, min_periods=20).max()
        matrices.update(returns=returns, advance=returns.gt(0).astype(float).where(returns.notna()),
            above=close.gt(ma).astype(float).where(close.notna() & ma.notna()),
            breakout=close.gt(high).astype(float).where(close.notna() & high.notna()))
        market = matrices["amount"].sum(axis=1, min_count=1).to_numpy()
        arrays = {field: matrix.to_numpy(dtype=float) for field, matrix in matrices.items()}
        positions = {symbol: position for position, symbol in enumerate(symbols)}
        groups = {}
        for row in member_rows:
            groups.setdefault((str(row.get("source") or "unknown"), str(row["sector_code"])), []).append(row)
        boards, details = [], {}
        for (source, code), population in sorted(groups.items()):
            original = sorted({row["symbol"] for row in population})
            members = [symbol for symbol in original if _supported(symbol)]
            columns = [positions[symbol] for symbol in members]
            expected = len(members)
            metrics, counts = {}, {}
            for field in ("returns", "advance", "above", "breakout", "amount"):
                local = arrays[field][:, columns]
                count = np.isfinite(local).sum(axis=1)
                usable = (count >= 5) & (count >= .8 * expected) if expected else np.zeros(len(dates), dtype=bool)
                total = np.nansum(local, axis=1)
                metrics[field] = np.where(usable, total if field == "amount" else total / np.maximum(count, 1), np.nan)
                counts[field] = count
            shares = np.divide(metrics["amount"], market, out=np.full(len(dates), np.nan), where=market > 0)
            features = _features(metrics["returns"], metrics["amount"], metrics["advance"], metrics["above"], metrics["breakout"], shares)
            history = []
            comparable_counts = []
            for position, day in enumerate(dates):
                comparable = np.zeros(expected, dtype=bool) if position == 0 else np.isfinite(arrays["amount"][position, columns]) & np.isfinite(arrays["amount"][position - 1, columns])
                count = int(comparable.sum())
                comparable_counts.append(count)
                usable = expected > 0 and count >= 5 and count / expected >= .8
                current = float(arrays["amount"][position, columns][comparable].sum()) if usable else None
                previous = float(arrays["amount"][position - 1, columns][comparable].sum()) if usable else None
                prior = metrics["amount"][max(0, position - 20):position]
                prior_mean = float(prior.mean()) if len(prior) == 20 and np.isfinite(prior).all() else None
                history.append({"date": day, "amount": _number(metrics["amount"][position]), "market_amount": _number(market[position]),
                    "market_share": _number(shares[position]), "return_1": _number(metrics["returns"][position]),
                    "amount_change": current - previous if usable else None,
                    "amount_change_pct": (current - previous) / previous if usable and previous > 0 else None,
                    "comparable_current_amount": current, "comparable_previous_amount": previous,
                    "prior20_mean_amount": prior_mean,
                    "relative_amount20": _number(metrics["amount"][position] / prior_mean) if prior_mean is not None and prior_mean > 0 else None,
                    "coverage": {"expected": expected, "available": int(counts["amount"][position]), "comparable": count}})
            latest = history[-1]
            board_id = f"{source}:sector_members:{code}"
            classification = taxonomy["boards"].get(code, {}) if source == "eastmoney" else {}
            board = {"board_id": board_id, "code": code, "name": str(population[0]["sector_name"]), "source": source,
                "kind": classification.get("kind", "mixed"), "taxonomy_verified": classification.get("verified", False),
                "amount": features["board_amount"], "amount_change": latest["amount_change"], "amount_change_pct": latest["amount_change_pct"],
                "return_1": features["board_return_1"], "relative_amount20": features["board_relative_amount20"],
                "market_share": features["board_market_amount_share"], "activity_score": None, "coverage": latest["coverage"],
                "metric_coverage": {field: {"available": int(count[-1]), "ratio": int(count[-1]) / expected if expected else 0.} for field, count in counts.items()},
                "source_member_count": len(original), "excluded_members": len(original) - expected,
                "membership_date": str(membership_date), "price_date": dates[-1], "calculation_scope": "sh_sz_a_v1",
                "membership_sha256": _digest(population), "projection_sha256": _digest(members),
                "training_eligible": False, **features}
            boards.append(board)
            member_details = [{"symbol": symbol, "in_calculation_scope": True,
                "amount": _number(arrays["amount"][-1, positions[symbol]]), "return_1": _number(arrays["returns"][-1, positions[symbol]])} for symbol in members]
            details[board_id] = {"history": history[-60:], "members": member_details, "mapping_status": "same_dataset_exact_members"}
        return boards, details, {"membership_date": str(membership_date), "market_amount": _number(market[-1]),
            "observed_market_members": int(np.isfinite(arrays["amount"][-1]).sum()),
            "identity": {"membership_content_sha256": _frame_hash(member_frame, ["source", "sector_code", "symbol"]), "taxonomy": taxonomy},
            "warnings": ["成员人口仅是该来源已发布成员行，来源全量完整性未认证。",
                "CNE日线来源保持原身份；数值有效不自动授予旧研究引擎成交价格资格。"]}

    @staticmethod
    def _source_prepare(prices, dates):
        frame = pd.DataFrame(prices.to_dicts())
        frame = frame.loc[frame.source.eq("ths")].copy()
        if frame.empty:
            raise ValueError("No THS board observations in pinned sector_bars generation")
        frame["trade_date"] = frame.trade_date.astype(str)
        boards, details = [], {}
        for (source, code), population in frame.groupby(["source", "sector_code"], sort=True):
            duplicate = population.duplicated("trade_date", keep=False)
            local = population.loc[~duplicate].set_index("trade_date").reindex(dates)
            numeric = local[["open", "high", "low", "close", "volume", "amount"]].apply(pd.to_numeric, errors="coerce")
            valid = (np.isfinite(numeric).all(axis=1) & numeric[["open", "high", "low", "close"]].gt(0).all(axis=1)
                & numeric[["volume", "amount"]].ge(0).all(axis=1)
                & numeric.high.ge(numeric[["open", "low", "close"]].max(axis=1))
                & numeric.low.le(numeric[["open", "high", "close"]].min(axis=1)))
            amount = numeric.amount.where(valid).to_numpy(dtype=float)
            close = numeric.close.where(valid)
            returns = (close / close.shift(1) - 1).to_numpy(dtype=float)
            history = []
            for position, day in enumerate(dates):
                prior = amount[max(0, position - 20):position]
                usable = position > 0 and np.isfinite(amount[position - 1:position + 1]).all()
                change = float(amount[position] - amount[position - 1]) if usable else None
                prior_mean = float(prior.mean()) if len(prior) == 20 and np.isfinite(prior).all() else None
                history.append({"date": day, "amount": _number(amount[position]), "return_1": _number(returns[position]),
                    "amount_change": change, "amount_change_pct": change / amount[position - 1] if usable and amount[position - 1] > 0 else None,
                    "market_amount": None, "market_share": None,
                    "prior20_mean_amount": prior_mean,
                    "relative_amount20": _number(amount[position] / prior_mean) if prior_mean is not None and prior_mean > 0 else None,
                    "coverage": {"expected": None, "available": None, "comparable": None}})
            latest = history[-1]
            prior = latest["prior20_mean_amount"]
            board_id = f"{source}:sector_bars:{code}"
            kinds = set(population.board_type.dropna())
            kind = next(iter(kinds)) if len(kinds) == 1 and next(iter(kinds)) in {"industry", "concept"} else "mixed"
            features = {field: None for field in FEATURE_COLUMNS}
            for count in (1, 5, 10, 20):
                values = _window(returns, count)
                features[f"board_return_{count}"] = _number(np.prod(1 + values) - 1) if values is not None else None
            relative = latest["amount"] / prior if latest["amount"] is not None and prior is not None and prior > 0 else None
            features.update(board_amount=latest["amount"], board_relative_amount20=relative)
            boards.append({"board_id": board_id, "code": str(code), "name": str(population.iloc[-1].sector_name),
                "kind": kind, "source": str(source), "amount": latest["amount"], "amount_change": latest["amount_change"],
                "amount_change_pct": latest["amount_change_pct"], "return_1": latest["return_1"], "relative_amount20": relative,
                "market_share": None, "activity_score": None, "coverage": latest["coverage"], "training_eligible": False,
                "mapping_status": "unsupported", "price_date": dates[-1], **features})
            details[board_id] = {"history": history[-60:], "members": [], "mapping_status": "unsupported"}
        return boards, details, {"identity": {}, "warnings": ["原生板块成交额单位source_native，未核实人民币换算，不显示亿元。",
            "原生板块与成员来源/代码体系不自动按名称映射；成员和市场份额不支持。"]}
