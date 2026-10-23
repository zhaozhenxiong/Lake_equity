"""Independent OOS-score research account using the existing Broker ledger."""

from __future__ import annotations

import hashlib
import inspect
import math
from pathlib import Path

import numpy as np
import pandas as pd

from my_strategy.execution.broker import BrokerSimulator
from my_strategy.execution.ledger import Ledger
from my_strategy.execution.order import Order
from my_strategy.services.position_simulation import _costs

from .execution import VERSION as EXECUTION_VERSION, SOURCES as EXECUTION_SOURCES
from .execution import PRICE_COLUMNS, EXECUTION_EVIDENCE_COLUMNS
from .execution import execution_evidence, execution_profile, round_shares, valuation_eligible


DEFAULTS = dict(
    top_k=10, rebalance_sessions=5, max_total_weight=.9, max_stock_weight=.2,
    initial_cash=1_000_000., commission=.0003, min_commission=5., slippage=.0005,
    transfer_fee=.00001, cost_stress=1., max_volume_participation=.01, require_verified_rules=False,
)


def _check_cancel(callback):
    if callback and callback():
        from lake_equity.jobs import JobCancelled
        raise JobCancelled("Portfolio backtest cancelled")


def _config(supplied):
    unknown = set(supplied or {}) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown portfolio parameters: " + ", ".join(sorted(unknown)))
    cfg = {**DEFAULTS, **(supplied or {})}
    for key in ("top_k", "rebalance_sessions"):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(key + " must be a positive integer")
    if not isinstance(cfg['require_verified_rules'], bool):
        raise ValueError('require_verified_rules must be boolean')
    for key in set(DEFAULTS) - {"top_k", "rebalance_sessions", "require_verified_rules"}:
        value = cfg[key]
        if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(key + " must be finite and nonnegative")
        cfg[key] = float(value)
    if cfg["initial_cash"] <= 0 or cfg["cost_stress"] <= 0:
        raise ValueError("initial_cash and cost_stress must be positive")
    if not 0 < cfg["max_stock_weight"] <= cfg["max_total_weight"] <= 1:
        raise ValueError("Require 0 < max_stock_weight <= max_total_weight <= 1")
    if not 0 < cfg["max_volume_participation"] <= 1:
        raise ValueError("max_volume_participation must be in (0,1]")
    return cfg


def _stamp(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("Missing prediction availability")
    return stamp.tz_localize("Asia/Shanghai") if stamp.tzinfo is None else stamp.tz_convert("Asia/Shanghai")


def _positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _sellable_shares(lots, day):
    return sum(lot["shares"] for lot in lots if lot["date"] < day)


def _consume_lots(lots, shares, day):
    for lot in lots:
        if lot["date"] >= day:
            continue
        count = min(lot["shares"], shares)
        lot["shares"] -= count
        shares -= count
        if not shares:
            break
    lots[:] = [lot for lot in lots if lot["shares"]]


def _implementation():
    objects = (backtest_scores, execution_evidence, BrokerSimulator, Ledger, Order, _costs)
    paths = {Path(inspect.getfile(obj)).resolve() for obj in objects}
    # CostModel is inherited by the dated costs helper, so bind its source too.
    from my_strategy.execution.cost_model import CostModel
    paths.add(Path(inspect.getfile(CostModel)).resolve())
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def _metrics(daily, trades, orders, initial_cash):
    equity = np.asarray([row["equity"] for row in daily], dtype=float)
    previous = np.r_[initial_cash, equity[:-1]]
    returns = equity / previous - 1
    peaks = np.maximum.accumulate(np.r_[initial_cash, equity])[1:]
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.
    notional = sum(row["shares"] * row["price"] for row in trades)
    return dict(
        initial_cash=initial_cash, ending_equity=float(equity[-1]),
        total_return=float(equity[-1] / initial_cash - 1),
        annualized_return=float((equity[-1] / initial_cash) ** (252 / len(equity)) - 1),
        max_drawdown=float((1 - equity / peaks).max()),
        sharpe=float(returns.mean() / std * np.sqrt(252)) if len(returns) >= 60 and std > 1e-12 else None,
        sharpe_reason=None if len(returns) >= 60 and std > 1e-12 else "insufficient_sessions_or_variation",
        trading_sessions=len(daily), fills=len(trades),
        fees=sum(row["fee"] for row in trades), traded_notional=notional,
        turnover=notional / float(equity.mean()),
        rejected_orders=sum(row["status"] == "rejected" for row in orders),
        partial_orders=sum(row["status"] == "partial" for row in orders),
        stale_valuation_days=sum(bool(row["stale_symbols"]) for row in daily),
        annualized_estimate=len(daily) < 252,
    )


def backtest_scores(prices: pd.DataFrame, predictions: pd.DataFrame, calendar: list[str],
                    config: dict | None = None, *, run_id: str, check_cancel=None) -> dict:
    """Rank each rebalance's OOS scores and trade only the next calendar open.

    Failed/partial orders expire on that open. Missing scores leave inventory
    marked and unchanged. This branch declares no independent stop-loss policy.
    """
    cfg = _config(config)
    _check_cancel(check_cancel)
    if not run_id:
        raise ValueError("A run_id is required")
    dates = [str(day) for day in calendar]
    if not dates or dates != sorted(set(dates)) or any(str(pd.Timestamp(day).date()) != day for day in dates):
        raise ValueError("Calendar must contain sorted unique ISO session dates")
    price_columns = {"date", "symbol", "open", "high", "low", "close", "volume", "amount"}
    score_columns = {"date", "symbol", "score", "model_name", "fold", "train_end", "available_at"}
    if not price_columns <= set(prices) or not score_columns <= set(predictions):
        raise ValueError("Prices or OOS predictions are missing required columns")
    scores = pd.DataFrame(predictions, copy=True)
    for frame in (scores,):
        frame["date"] = frame.date.astype(str).str[:10]
        frame["symbol"] = frame.symbol.astype(str)
        if frame.duplicated(["date", "symbol"]).any():
            raise ValueError("Duplicate symbol/date observations")
        if not set(frame.date) <= set(dates):
            raise ValueError("Observations outside the verified calendar")
    if scores.empty or scores.model_name.nunique(dropna=False) != 1:
        raise ValueError("Require nonempty OOS predictions for exactly one model")
    if scores[["model_name", "fold", "train_end", "available_at"]].isna().any().any():
        raise ValueError("OOS predictions require model, fold, training end and availability")
    scores["train_end"] = pd.to_datetime(scores.train_end, format="%Y-%m-%d", errors="raise").dt.strftime("%Y-%m-%d")
    if (scores.train_end >= scores.date).any():
        raise ValueError("In-sample predictions: train_end must precede decision date")
    scores["score"] = pd.to_numeric(scores.score, errors="coerce")
    if not np.isfinite(scores.score).all():
        raise ValueError("OOS scores must be finite")
    positions = {day: index for index, day in enumerate(dates)}
    first = min(scores.date)
    first_index = positions[first]
    prefix_start = dates[max(0, first_index - 1)]
    # A model's training panel can contain years and dozens of feature columns.
    # The Broker needs only this execution window, its preceding quote, and
    # explicit execution evidence; do not duplicate the full panel into dicts.
    source_prices = pd.DataFrame(prices, copy=False)
    source_days = source_prices.date.astype(str).str[:10]
    within = source_days.between(prefix_start, dates[-1]) & source_prices.symbol.isin(set(scores.symbol))
    retained = [column for column in (*PRICE_COLUMNS, *EXECUTION_EVIDENCE_COLUMNS) if column in source_prices]
    bars = source_prices.loc[within, retained].copy()
    bars['date'] = source_days.loc[within].to_numpy()
    bars['symbol'] = bars.symbol.astype(str)
    if bars.duplicated(['date', 'symbol']).any():
        raise ValueError('Duplicate symbol/date observations')
    if not set(bars.date) <= set(dates):
        raise ValueError('Observations outside the verified calendar')
    del source_prices, source_days, within
    for number, row in enumerate(scores.itertuples()):
        if check_cancel and number % 1024 == 0:
            _check_cancel(check_cancel)
        available = _stamp(row.available_at)
        if available < _stamp(row.date + "T15:00:00+08:00"):
            raise ValueError("Close-feature scores cannot be available before the decision close")
        index = positions[row.date]
        if index + 1 < len(dates) and available >= _stamp(dates[index + 1] + "T09:30:00+08:00"):
            raise ValueError("Prediction unavailable before its next executable open")

    lookup = {(row["date"], row["symbol"]): row for row in bars.to_dict("records")}
    grouped = {day: group for day, group in scores.groupby("date", sort=True)}
    last_score = max(scores.date)
    sessions = [day for day in dates if day >= first]
    cost_config = {**cfg, "cost_version": "flat_v1"}
    broker = BrokerSimulator(initial_cash=cfg["initial_cash"], run_id=run_id)
    marks, lots, orders, daily, decisions = {}, {}, [], [], []
    pending = None
    for day in sessions:
        _check_cancel(check_cancel)
        index = positions[day]
        previous_day = dates[index - 1] if index else None
        held = {symbol for symbol, shares in broker.positions.items() if shares}
        opening_marks = {symbol: marks[symbol] for symbol in held}
        if pending:
            for symbol in held | set(pending["weights"]):
                row = lookup.get((day, symbol))
                if row and _positive(row.get("open")):
                    opening_marks[symbol] = float(row["open"])
            equity = broker.equity(opening_marks)
            wanted = {symbol: int(equity * weight / opening_marks[symbol]) // execution_profile(symbol, day)['quantity_step'] * execution_profile(symbol, day)['quantity_step']
                      for symbol, weight in pending["weights"].items() if symbol in opening_marks}
            candidates = []
            for symbol in sorted(held | set(pending["weights"])):
                current = lookup.get((day, symbol))
                if symbol in pending["weights"] and (not current or not _positive(current.get("open"))):
                    orders.append(dict(order_id=f"{run_id}-{len(orders) + 1}", signal_date=pending["date"],
                        date=day, symbol=symbol, side="REBALANCE", requested_shares=None,
                        target_weight=pending["weights"][symbol], filled_shares=0,
                        status="rejected", reason="missing_open", constraints=[],
                        execution_evidence=execution_profile(symbol, day, current),
                        expires_at=day + "T09:30:00+08:00"))
                    continue
                target = wanted.get(symbol, 0)
                difference = target - broker.shares(symbol)
                if difference:
                    candidates.append((difference > 0, symbol, abs(difference)))
                elif not broker.shares(symbol) and symbol in pending['weights']:
                    candidates.append((True, symbol, target))
            for is_buy, symbol, requested in sorted(candidates):
                _check_cancel(check_cancel)
                side = "BUY" if is_buy else "SELL"
                original = lookup.get((day, symbol))
                row = dict(original) if original else None
                prior = lookup.get((previous_day, symbol)) if previous_day else None
                if row is not None:
                    row["_previous_close"] = prior.get("close") if prior else None
                    row['_previous_bar_valid'] = prior.get('bar_valid') if prior else None
                costs = _costs(cost_config, day)
                reference = float(row["open"]) if row and _positive(row.get("open")) else 0.
                price = costs.execution_price(side, reference)
                execution = execution_evidence(symbol, row, side, day, price,
                    require_verified_rules=cfg['require_verified_rules'])
                profile = execution_profile(symbol, day, row)
                failure = execution['reason']
                previous_volume = prior.get("volume") if prior else None
                participation = int(float(previous_volume) * cfg["max_volume_participation"]) if _positive(previous_volume) else 0
                if not failure and participation <= 0:
                    failure = "prior_session_liquidity_unavailable"
                quantity = min(requested, participation, profile['max_order'])
                constraints = []
                if participation < requested:
                    constraints.append("prior_volume_participation_limit")
                if requested > profile['max_order']:
                    constraints.append('exchange_order_size_limit')
                if not is_buy:
                    sellable = _sellable_shares(lots.get(symbol, []), day)
                    quantity = min(quantity, sellable)
                    if quantity < min(requested, participation):
                        constraints.append("t_plus_one_locked")
                else:
                    # Recheck caps after sales, fees and any unfilled inventory.
                    current_equity = broker.equity(opening_marks)
                    invested = current_equity - broker.cash
                    budget = min(broker.cash,
                                 max(0., current_equity * cfg["max_total_weight"] - invested),
                                 max(0., current_equity * cfg["max_stock_weight"] - broker.shares(symbol) * reference))
                    affordable = int(budget / price) if price > 0 else 0
                    quantity = min(quantity, affordable)
                    quantity = round_shares(symbol, side, quantity, day=day)
                    while quantity > 0 and quantity * price + costs.fees(side, quantity * price) > budget + 1e-8:
                        quantity = round_shares(symbol, side, quantity - profile['quantity_step'], day=day)
                    if quantity < min(requested, participation):
                        constraints.append("cash_or_weight_cap")
                if not is_buy:
                    quantity = round_shares(symbol, side, quantity, day=day,
                        held=broker.shares(symbol), sellable=sellable)
                if not failure and quantity <= 0:
                    failure = constraints[-1] if constraints else "below_minimum_order_size"
                order = dict(order_id=f"{run_id}-{len(orders) + 1}", signal_date=pending["date"],
                             date=day, symbol=symbol, side=side, requested_shares=requested,
                             target_weight=pending["weights"].get(symbol, 0.), filled_shares=0,
                             status="rejected", reason=failure, constraints=constraints, execution_evidence=execution,
                             expires_at=day + "T09:30:00+08:00")
                if not failure:
                    broker.cost_model = costs
                    fill = broker.submit(Order(date=day, symbol=symbol, side=side,
                        shares=quantity, price=price, reason="oos_rank_target_weight",
                        target_weight=order["target_weight"], signal_date=pending["date"],
                        applied_slippage=costs.resolve_slippage()), portfolio_prices=opening_marks)
                    if fill is None:
                        order["reason"] = "broker_rejected_cash_or_inventory"
                    else:
                        order.update(status="filled" if quantity == requested else "partial",
                                     filled_shares=quantity, price=price, fee=fill.fee,
                                     reason=None if quantity == requested else ",".join(constraints))
                        if is_buy:
                            lots.setdefault(symbol, []).append(dict(date=day, shares=quantity))
                            marks.setdefault(symbol, reference)
                        else:
                            _consume_lots(lots[symbol], quantity, day)
                orders.append(order)
            pending = None

        stale = []
        for symbol, shares in broker.positions.items():
            if not shares:
                continue
            row = lookup.get((day, symbol))
            if valuation_eligible(row):
                marks[symbol] = float(row["close"])
            else:
                stale.append(symbol)
        daily.append(dict(date=day, cash=float(broker.cash), equity=float(broker.equity(marks)),
                          positions={symbol: shares for symbol, shares in broker.positions.items() if shares},
                          stale_symbols=sorted(stale)))
        if day <= last_score and (index - first_index) % cfg["rebalance_sessions"] == 0:
            if day not in grouped:
                decisions.append(dict(date=day, status="skipped", reason="no_current_oos_scores_inventory_unchanged"))
                continue
            selected = grouped[day].sort_values(["score", "symbol"], ascending=[False, True]).head(cfg["top_k"])
            weight = min(cfg["max_total_weight"] / len(selected), cfg["max_stock_weight"])
            weights = {symbol: weight for symbol in selected.symbol}
            execute_on = dates[index + 1] if index + 1 < len(dates) else None
            decisions.append(dict(date=day, status="scheduled" if execute_on else "unexecuted",
                                  reason=None if execute_on else "no_next_verified_session",
                                  execute_on=execute_on, weights=weights,
                                  folds=sorted(selected.fold.astype(str).unique().tolist())))
            if execute_on:
                pending = dict(date=day, weights=weights)
    trades = broker.ledger.to_frame().to_dict("records")
    return dict(schema="lake_target_weight_backtest_v1", run_id=run_id, status="completed",
                research_only=True, strict_history_eligible=False, paper_allocation_eligible=False,
                independent_forward_status="pending", account_scope="shared_cash_account",
                evaluation_start=sessions[0], evaluation_end=sessions[-1],
                price_materialization=dict(start=prefix_start, end=dates[-1], rows=len(bars), columns=retained),
                model_name=str(scores.model_name.iloc[0]), config=cfg,
                execution_rules=dict(version=EXECUTION_VERSION, sources=EXECUTION_SOURCES,
                    require_verified_rules=cfg['require_verified_rules'],
                    order_semantics='synthetic_opening_fill_not_verified_exchange_instruction'),
                implementation_sha256=_implementation(), cost_model=dict(
                    version="flat_v1", stress=cfg["cost_stress"], dated_stamp_tax=True,
                    transfer_fee_stress=False, commission_and_minimum_and_slippage_stressed=True),
                decisions=decisions, orders=orders,
                rejections=[row for row in orders if row["status"] == "rejected"],
                daily=daily, trades=trades, ledger=trades,
                metrics=_metrics(daily, trades, orders, cfg["initial_cash"]),
                terminal_state=dict(cash=float(broker.cash),
                    positions={symbol: shares for symbol, shares in broker.positions.items() if shares},
                    lots={symbol: entries for symbol, entries in lots.items() if entries},
                    last_known_marks={symbol: marks[symbol] for symbol, shares in broker.positions.items() if shares},
                    forced_liquidation=False),
                limitations=[
                    "Research reconstruction: input lacks complete historical executable-price and corporate-action evidence.",
                    "Dated research rules cover main-board 60*.SH/00*.SZ, STAR 688*.SH and ChiNext 30*.SZ; other markets are rejected.",
                    "Regular-session limits: mainboard 10%; known ST 5% before 2026-07-06, then 10%; STAR 20%; ChiNext 10% before 2020-08-24, then 20%.",
                    "Unknown historical ST/IPO/company-action states stay unknown and are recorded per order; regular-session fills are research assumptions.",
                    "Known IPO/relisting/delisting exceptions or invalid/corporate-action bars are rejected; supplied strict rules do not grant paper qualification.",
                    "STAR orders use a 200-share minimum and 1-share increments; mainboard/ChiNext buys use 100-share lots; full residual liquidation is retained.",
                    "Opening fills approximate execution; prior-session volume is the participation cap, not proof of opening liquidity.",
                    "Missing open expires that order; no delayed fill or future-price substitution occurs.",
                    "Missing close carries the last known mark and is counted as stale valuation.",
                    "Missing current scores leave inventory unchanged; this branch has no independent stop-loss policy.",
                    "Open positions remain at the end; returns are price-account research results, not certified total returns.",
                ])
