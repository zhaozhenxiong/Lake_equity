"""Explicit next-session conditional plan built from a completed model backtest.

This is a controlled research action: it is created only after an explicit
``confirm:true`` request, reads only the saved backtest report/artifacts and the
verified trading calendar, and never writes the trading account/plan tables.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pandas as pd
from filelock import FileLock

from .execution import execution_profile, round_shares
from .registry import canonical
from .trading import DEFAULT_RISK


ALLOWED = {'confirm'}
DEFAULT_OFFSET = .005


def _now():
    return datetime.now().astimezone().isoformat()


def _day(value, name):
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise ValueError(name + ' must be an ISO date')
    return value


def _artifact_sha(report, name):
    record = (report.get('artifacts') or {}).get(name)
    if isinstance(record, dict):
        return record.get('sha256')
    return record


def _fees(side, quantity, price, day, risk):
    if not quantity:
        return 0.
    notional = quantity * price
    commission = max(float(risk.get('min_commission', 5.)), notional * float(risk.get('commission', .0003)))
    stamp = (.0005 if day >= '2023-08-28' else .001) if side == 'SELL' else 0.
    return commission + notional * (float(risk.get('transfer_fee', .00001)) + stamp)


class BacktestPlanService:
    def __init__(self, root):
        from .workflow import LearningWorkflow
        self.root = Path(root).resolve()
        self.workflow = LearningWorkflow(self.root)

    def create(self, run_id, params):
        body = dict(params or {})
        unknown = set(body) - ALLOWED
        if unknown:
            raise ValueError('Unknown backtest plan parameters: ' + ', '.join(sorted(unknown)))
        if body.get('confirm') is not True:
            raise ValueError('生成次日计划需要显式确认（confirm=true）')
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('A backtest run identity is required')
        report = self.workflow.read_run(run_id)
        if report.get('family') != 'model_backtest_v1':
            raise ValueError('Only a saved model backtest can produce this research plan')
        if report.get('status') != 'completed':
            raise ValueError('The source backtest is not completed')
        directory = self.workflow.directory / run_id
        signal_date = _day(report.get('actual_end'), 'actual_end')
        scores, prices = self._artifacts(report, directory)
        scores = scores.loc[scores.date.astype(str).str[:10].eq(signal_date)]
        if scores.empty:
            raise ValueError('No saved scores on the backtest signal date')
        prices = prices.loc[prices.date.astype(str).str[:10].eq(signal_date)]
        if prices.empty:
            raise ValueError('No saved prices on the backtest signal date')
        session = self._session(report.get('read_ref'))
        execution_date = self._next_session(session, signal_date)
        plan = self._plan(report, scores, prices, signal_date, execution_date)
        lease = self.root / 'var/learning/compute.lock'
        lease.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lease), timeout=0):
            return self._save(plan, report, signal_date)

    # -- data --------------------------------------------------------------
    def _artifacts(self, report, directory):
        frames = {}
        for name in ('scores.parquet', 'prices.parquet'):
            path = directory / name
            expected = _artifact_sha(report, name)
            if not expected or not path.is_file():
                raise ValueError('Saved backtest artifact is missing: ' + name)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Saved backtest artifact SHA256 mismatch: ' + name)
            frames[name] = pd.read_parquet(path)
        return frames['scores.parquet'], frames['prices.parquet']

    def _session(self, read_ref=None):
        from .dataset import DatasetSession
        return DatasetSession(self.root, read_ref)

    def _next_session(self, session, signal_date):
        horizon = (date.fromisoformat(signal_date) + timedelta(days=120)).isoformat()
        calendar = self.workflow._sessions(session, horizon, {})
        later = [day for day in calendar if day > signal_date]
        if not later:
            raise ValueError('No verified trading session follows the backtest signal date')
        session.verify()
        return later[0]

    @staticmethod
    def _next_day(value):
        return (date.fromisoformat(value) + timedelta(days=1)).isoformat()

    # -- plan --------------------------------------------------------------
    def _plan(self, report, scores, prices, signal_date, execution_date):
        portfolio = report.get('portfolio') or {}
        terminal = portfolio.get('terminal_state') or {}
        config = {**DEFAULT_RISK, **(portfolio.get('config') or {})}
        cash = float(terminal.get('cash') or 0.)
        positions = {str(symbol): int(shares) for symbol, shares in (terminal.get('positions') or {}).items() if shares}
        marks = {str(symbol): float(value) for symbol, value in (terminal.get('last_known_marks') or {}).items()
                 if value is not None}
        reference = {}
        for row in prices.to_dict('records'):
            try:
                reference[str(row['symbol'])] = dict(close=float(row['close']), volume=float(row.get('volume') or 0.))
            except (TypeError, ValueError):
                continue
        top_k = max(1, int(config.get('top_k') or 10))
        ranked = scores.sort_values(['score', 'symbol'], ascending=[False, True]).drop_duplicates('symbol')
        selected = [str(symbol) for symbol in ranked['symbol'].tolist()[:top_k]]
        equity = cash + sum(shares * marks.get(symbol, 0.) for symbol, shares in positions.items())
        target_weight = min(float(config.get('max_stock_weight') or .1),
                            float(config.get('max_total_weight') or .8) / max(1, len(selected)))
        targets = {symbol: target_weight for symbol in selected}
        orders, holds, proceeds = [], [], 0.
        candidates = sorted(set(positions) | set(targets))
        missing_marks = [symbol for symbol in positions if symbol not in marks or not marks[symbol]]
        for symbol in candidates:
            current = positions.get(symbol, 0)
            mark = reference.get(symbol)
            if not mark or not mark['close']:
                holds.append(dict(symbol=symbol, action='WAIT', reason='missing_reference_close', total_shares=current))
                continue
            close = mark['close']
            target = round_shares(symbol, 'BUY', int(equity * targets.get(symbol, 0.) / close), day=execution_date) if symbol in targets else 0
            difference = target - current
            if not difference:
                holds.append(dict(symbol=symbol, action='HOLD' if current else 'WAIT', reason='at_target', total_shares=current))
                continue
            side = 'BUY' if difference > 0 else 'SELL'
            profile = execution_profile(symbol, execution_date)
            volume = mark['volume']
            constraints = ['recheck_next_session_open_and_daily_limits', 'suspension_and_corporate_actions_must_be_rechecked']
            if volume <= 0:
                holds.append(dict(symbol=symbol, action='WAIT', reason='prior_session_liquidity_unavailable', total_shares=current))
                continue
            participation = int(volume * float(config.get('max_volume_participation') or .01))
            wanted = min(abs(difference), participation, profile['max_order'])
            if wanted < abs(difference):
                constraints.append('liquidity_or_exchange_size_cap')
            price = round(close * (1 - DEFAULT_OFFSET if side == 'BUY' else 1 + DEFAULT_OFFSET), 2)
            price = max(.01, price)
            if side == 'SELL':
                quantity = round_shares(symbol, side, wanted, day=execution_date, held=current, sellable=current)
                if quantity < wanted:
                    constraints.append('t_plus_one_or_frozen_inventory')
                proceeds += max(0., quantity * price - _fees(side, quantity, price, execution_date, config))
                funding = 'existing_inventory'
            else:
                invested = equity - cash
                exposure_budget = max(0., equity * float(config.get('max_total_weight') or .8) - invested)
                budget = min(max(0., cash + proceeds), exposure_budget)
                quantity = round_shares(symbol, side, min(wanted, int(budget / price)), day=execution_date)
                while quantity and quantity * price + _fees(side, quantity, price, execution_date, config) > budget:
                    quantity = round_shares(symbol, side, quantity - profile['quantity_step'], day=execution_date)
                if quantity < wanted:
                    constraints.append('cash_and_fee_limit')
                funding = 'existing_cash' if quantity * price + _fees(side, quantity, price, execution_date, config) <= max(0., cash) else 'conditional_sale_proceeds'
            if quantity <= 0:
                holds.append(dict(symbol=symbol, action='HOLD' if current else 'WAIT', reason='below_minimum_order_size', total_shares=current))
                continue
            orders.append(dict(order_id='', symbol=symbol, side=side, quantity=int(quantity), requested_quantity=int(abs(difference)),
                               current_shares=int(current), target_weight=targets.get(symbol, 0.), reference_close=close,
                               reference_date=signal_date, order_type='CONDITIONAL_NEXT_SESSION_LIMIT', limit_price=price,
                               estimated_notional=round(quantity * price, 2),
                               estimated_fees=round(_fees(side, quantity, price, execution_date, config), 2),
                               funding=funding, constraints=constraints, reason='frozen_model_rank_rebalance',
                               status='planned', valid_session=execution_date,
                               expires_at=execution_date + 'T15:00:00+08:00'))
        orders.sort(key=lambda row: (row['side'] == 'BUY', row['symbol']))
        payload = dict(schema='backtest_conditional_plan_v1', source_backtest_run_id=report['run_id'],
                       signal_date=signal_date, execution_date=execution_date,
                       next_session_source='verified_trading_calendar',
                       account_scope='backtest_terminal_research_account',
                       account=dict(cash=round(cash, 2), positions=positions, lots=terminal.get('lots') or {},
                                    last_known_marks=marks),
                       risk=config, selected=selected, orders=orders, holds=holds,
                       valuation_missing_symbols=missing_marks,
                       qualification=dict(research_only=True, broker_connected=False, automatic_orders=False,
                                          executable_order_certified=False),
                       limitations=['Quantities and limit prices are conditional estimates from the signal close, not executed trades.',
                                    'Uses the backtest terminal research account; it is not a broker account and no account/plan record is written.',
                                    'No future open/high/low/volume is used to construct this plan.',
                                    'A limit-price buffer of 0.5% is a research assumption; recheck the next open, limits and corporate actions.'],
                       source_artifacts={'scores_sha256': _artifact_sha(report, 'scores.parquet'),
                                         'prices_sha256': _artifact_sha(report, 'prices.parquet')})
        payload['orders'] = [dict(order, order_id=payload['source_backtest_run_id'] + '-plan-' + str(index + 1))
                             for index, order in enumerate(orders)]
        return payload

    def _save(self, payload, source, signal_date):
        identity, working = self.workflow._start('bplan')
        payload['plan_id'] = identity
        payload['run_id'] = identity
        payload['created_at'] = _now()
        (working / 'plan.json').write_text(canonical(payload), encoding='utf-8')
        source_model = source.get('model_id') or next(iter(source.get('model_ids') or []), None)
        source_report = self.workflow.directory / source['run_id'] / 'report.json'
        source_report_sha = hashlib.sha256(source_report.read_bytes()).hexdigest() if source_report.is_file() else None
        report = dict(schema='backtest_conditional_plan_v1',
                      family='backtest_plan_v1', kind='bplan', run_id=identity, plan_id=identity,
                      status='completed', created_at=payload['created_at'],
                      spec=dict(source_backtest_run_id=source['run_id'], signal_date=signal_date,
                                execution_date=payload['execution_date']),
                      read_ref=source.get('read_ref'), as_of=signal_date,
                      model_id=source_model, model_ids=[source_model] if source_model else [],
                      source_backtest_run_id=source['run_id'], signal_date=signal_date,
                      execution_date=payload['execution_date'],
                      next_session_source=payload['next_session_source'],
                      account_scope=payload['account_scope'],
                      orders=payload['orders'], holds=payload['holds'], risk=payload['risk'],
                      selected=payload['selected'], account=payload['account'],
                      valuation_missing_symbols=payload['valuation_missing_symbols'],
                      source_artifacts={'scores_sha256': payload['source_artifacts']['scores_sha256'],
                                        'prices_sha256': payload['source_artifacts']['prices_sha256'],
                                        'report_sha256': source_report_sha},
                      qualification=payload['qualification'],
                      limitations=payload['limitations'], plan=payload,
                      _symbols=sorted(set(payload['account'].get('positions') or {}) | set(payload['selected'])))
        return self.workflow._save(report, working)
