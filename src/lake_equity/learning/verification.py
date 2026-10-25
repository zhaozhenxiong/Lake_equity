"""Pure reconciliation math for post-release forward signals.

Every number here is computed from already-published bars. A missing future
session, an invalid bar or a suspended execution leaves an explicit reason
instead of a zero or a substitute quote, so the ledger cannot silently score a
signal that could not have been executed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .dataset import BAR_COLUMNS, _valid_bars

COMMISSION = .0003
TRANSFER_FEE = .00001
STAMP_SELL = .0005
SLIPPAGE = .0005
SUSPEND_KEYS = ('suspended', 'is_suspended')


def round_trip_cost(multiplier=1.):
    """Ratio cost for one buy plus one sell at the given stress multiplier."""
    return round(float(multiplier) * (2 * COMMISSION + 2 * TRANSFER_FEE + STAMP_SELL + 2 * SLIPPAGE), 8)


def _truthful_state(row, keys):
    if row is None:
        return None
    getter = getattr(row, 'get', None)
    if getter is None:
        return None
    for key in keys:
        value = getter(key)
        if isinstance(value, (bool, int, np.bool_, np.integer)) and value in (0, 1):
            return bool(value)
    return None


def _dated(frame):
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=['symbol', 'date', *BAR_COLUMNS, 'source'])
    result = frame.copy()
    result['date'] = result.date.astype(str).str[:10]
    return result


def realized_return(bars, symbol, day, futures, horizon, cost_multiplier=2.):
    """Executable open-to-close return for one signal row (day -> futures[-1])."""
    exec_day, exit_day = futures[0], futures[-1]
    outcome = dict(symbol=symbol, exec_day=exec_day, exit_day=exit_day, horizon=int(horizon),
                   exec_open=None, exit_close=None, ret_gross=None, ret_net=None, ret_net_2x=None,
                   executable=False, status='pending', reason=None)
    frame = _dated(bars)
    rows = frame.loc[frame.symbol.eq(symbol)].set_index('date')
    if rows.index.duplicated().any():
        raise ValueError('Duplicate published bars for one symbol and session')
    if exec_day not in rows.index or exit_day not in rows.index:
        outcome['reason'] = 'future_session_not_published'
        return outcome
    pair = rows.loc[[exec_day, exit_day]].reset_index()
    valid = _valid_bars(pair)
    execution, exit_row = rows.loc[exec_day], rows.loc[exit_day]
    if _truthful_state(execution, SUSPEND_KEYS):
        outcome.update(status='unexecutable', reason='suspended_on_execution_session')
        return outcome
    if not bool(valid.iloc[0]):
        outcome.update(status='unexecutable', reason='execution_bar_invalid')
        return outcome
    if not bool(valid.iloc[1]):
        outcome.update(status='unexecutable', reason='exit_bar_invalid')
        return outcome
    exec_open, exit_close = float(execution.open), float(exit_row.close)
    if not np.isfinite(exec_open) or not np.isfinite(exit_close) or exec_open <= 0 or exit_close <= 0:
        outcome.update(status='unexecutable', reason='non_positive_execution_price')
        return outcome
    gross = exit_close / exec_open - 1
    outcome.update(exec_open=exec_open, exit_close=exit_close, ret_gross=float(gross),
                   ret_net=float(gross - round_trip_cost(1.)), ret_net_2x=float(gross - round_trip_cost(cost_multiplier)),
                   executable=True, status='ok', reason=None)
    return outcome


def _spearman(x, y):
    if len(x) < 3:
        return None
    left, right = pd.Series(list(x), dtype='float64'), pd.Series(list(y), dtype='float64')
    if left.std(ddof=0) == 0 or right.std(ddof=0) == 0:
        return None
    value = np.corrcoef(left.rank(), right.rank())[0, 1]
    return float(value) if np.isfinite(value) else None


def factor_direction(score, median):
    if score is None or median is None or score == median:
        return None
    return 1 if score > median else -1


def kronos_direction(median):
    if median is None or median == 0:
        return None
    return 1 if median > 0 else -1


def bucket_name(factor_dir, kronos_dir):
    if factor_dir == 1 and kronos_dir == 1:
        return 'A'
    if factor_dir == 1 and kronos_dir == -1:
        return 'B'
    if factor_dir == -1 and kronos_dir == 1:
        return 'C'
    return 'D'


def _top_set(rows, key, count):
    ranked = sorted([row for row in rows if row.get(key) is not None], key=lambda row: (-row[key], row['symbol']))
    return {row['symbol'] for row in ranked[:count]}


def prediction_agreement(predictions, top_k=10):
    """Prediction-time factor/Kronos agreement; no outcome is required."""
    scored = [row for row in predictions if row.get('factor_score') is not None]
    paired = [row for row in scored if row.get('kronos_median') is not None]
    if not paired:
        return dict(n=0, sign_agree=None, rank_agree=None, topk_overlap=None, disagreement_rate=None)
    median = float(np.median([row['factor_score'] for row in scored]))
    pairs = [(factor_direction(row['factor_score'], median), kronos_direction(row['kronos_median'])) for row in paired]
    pairs = [(factor, kronos) for factor, kronos in pairs if factor is not None and kronos is not None]
    sign_agree = float(np.mean([factor == kronos for factor, kronos in pairs])) if pairs else None
    count = max(1, min(int(top_k), len(paired)))
    factor_top = _top_set(paired, 'factor_score', count)
    kronos_top = _top_set(paired, 'kronos_median', count)
    base = min(len(factor_top), len(kronos_top))
    return dict(n=len(paired), sign_agree=sign_agree,
                rank_agree=_spearman([row['factor_score'] for row in paired], [row['kronos_median'] for row in paired]),
                topk_overlap=(len(factor_top & kronos_top) / base) if base else None,
                disagreement_rate=(1 - sign_agree) if sign_agree is not None else None)


def cross_section(predictions, realized, top_k=10, cost_multiplier=2.):
    """Cross-sectional metrics for one signal day.

    ``predictions`` items: symbol, factor_score, factor_rank, kronos_median,
    expected_return. ``realized`` maps symbol to :func:`realized_return` output.
    """
    rows = []
    for row in predictions:
        outcome = realized.get(row['symbol']) or {}
        merged = dict(row)
        merged.update(ret_gross=outcome.get('ret_gross'), ret_net_2x=outcome.get('ret_net_2x'),
                      executable=bool(outcome.get('executable')), status=outcome.get('status'),
                      reason=outcome.get('reason'))
        rows.append(merged)
    executable = [row for row in rows if row['executable'] and row.get('ret_gross') is not None]
    scored = [row for row in executable if row.get('factor_score') is not None]
    scores = [row['factor_score'] for row in scored]
    returns = [row['ret_gross'] for row in scored]
    median_score = float(np.median(scores)) if scores else None
    paired = [row for row in scored if row.get('kronos_median') is not None]
    pairs = [(factor_direction(row['factor_score'], median_score), kronos_direction(row['kronos_median'])) for row in paired]
    pairs = [(factor, kronos) for factor, kronos in pairs if factor is not None and kronos is not None]
    sign_agree = float(np.mean([factor == kronos for factor, kronos in pairs])) if pairs else None
    count = max(1, min(int(top_k), len(executable))) if executable else 0
    factor_top = _top_set(scored, 'factor_score', count) if count else set()
    actual_top = {row['symbol'] for row in sorted(executable, key=lambda row: (-row['ret_gross'], row['symbol']))[:count]} if count else set()
    kronos_top = _top_set(paired, 'kronos_median', min(count, len(paired))) if count and paired else set()
    overlap_base = min(len(factor_top), len(kronos_top))
    buckets = {name: dict(count=0, wins=0, total=0.) for name in ('A', 'B', 'C', 'D')}
    for row in paired:
        factor_dir = factor_direction(row['factor_score'], median_score)
        k_dir = kronos_direction(row['kronos_median'])
        if factor_dir is None or k_dir is None:
            continue
        bucket = buckets[bucket_name(factor_dir, k_dir)]
        bucket['count'] += 1
        bucket['wins'] += int(row['ret_gross'] > 0)
        bucket['total'] += row['ret_gross']
    for bucket in buckets.values():
        number = bucket['count']
        bucket['mean_ret'] = bucket['total'] / number if number else None
        bucket['hit_rate'] = bucket['wins'] / number if number else None
        bucket.pop('wins', None)
        bucket.pop('total', None)
    expected = [(row['expected_return'], row['ret_gross']) for row in executable
                if row.get('expected_return') is not None]
    mae = float(np.mean([abs(expected_value - actual) for expected_value, actual in expected])) if expected else None
    return dict(
        n=len(executable), requested=len(rows), coverage=len(executable) / len(rows) if rows else 0.,
        rank_ic=_spearman(scores, returns),
        kronos_rank_ic=_spearman([row['kronos_median'] for row in paired], [row['ret_gross'] for row in paired]),
        rank_agree=_spearman([row['factor_score'] for row in paired], [row['kronos_median'] for row in paired]),
        sign_agree=sign_agree,
        disagreement_rate=(1 - sign_agree) if sign_agree is not None else None,
        hit_rate=float(np.mean([row['ret_gross'] > 0 for row in executable])) if executable else None,
        topk_precision=(len(factor_top & actual_top) / count) if count else None,
        topk_overlap=(len(factor_top & kronos_top) / overlap_base) if overlap_base else None,
        mean_ret=float(np.mean(returns)) if returns else None,
        median_ret=float(np.median(returns)) if returns else None,
        mean_ret_net_2x=float(np.mean([row['ret_net_2x'] for row in executable])) if executable else None,
        mae=mae, buckets=buckets,
        pending=[row['symbol'] for row in rows if row.get('status') == 'pending'],
        unexecutable=[dict(symbol=row['symbol'], reason=row.get('reason'))
                      for row in rows if row.get('status') == 'unexecutable'],
    )
