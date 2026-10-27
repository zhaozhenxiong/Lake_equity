"""Read-only interpretation of immutable forward observations."""
from datetime import datetime
import hashlib
import json
from zoneinfo import ZoneInfo

import numpy as np


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def qualify(record, row):
    reasons = []
    day = record.get('day', '')
    if record.get('origin') != 'live':
        reasons.append('historical_backfill')
    if not record.get('release_id') or not record.get('model_id'):
        reasons.append('missing_model_identity')
    if not record.get('information_cutoff') or record['information_cutoff'] >= day:
        reasons.append('model_information_not_prior')
    futures = record.get('following_sessions') or []
    try:
        created = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
        if created.tzinfo is None:
            raise ValueError('unqualified local timestamp')
        zone = ZoneInfo('Asia/Shanghai')
        close = datetime.fromisoformat(day + 'T15:00:00').replace(tzinfo=zone)
        opening = datetime.fromisoformat(futures[0] + 'T09:30:00').replace(tzinfo=zone)
        if not close <= created < opening:
            reasons.append('record_not_before_target_open')
    except (KeyError, IndexError, ValueError, TypeError):
        reasons.append('missing_creation_or_target_time')
    if not row.get('n'):
        reasons.append('no_verified_observations')
    return {'live_eligible': not reasons, 'reasons': reasons}


def annotate(row, record):
    horizon = row['horizon']
    row['origin'] = record.get('origin', 'unknown')
    row['release_id'] = record.get('release_id')
    row['model_id'] = record.get('model_id')
    row['model_horizon'] = record.get('model_horizon')
    row['group'] = [record.get(key) for key in ('release_id', 'model_id', 'series_id', 'model_version')]
    row['group'] += [horizon, row.get('scope'), row['origin']]
    protocol = {key: record.get(key) for key in ('rule_version', 'model_horizon', 'top_k', 'implementation_sha256')}
    protocol['kronos'] = (record.get('kronos') or {}).get('params')
    protocol['symbols'] = sorted(record.get('symbols') or [])
    row['group'].append(hashlib.sha256(json.dumps(protocol, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
    row['qualification'] = qualify(record, row)
    row['target'] = {
        'horizon': horizon, 'realized_formula': 'close[T+h]/open[T+1]-1',
        'kronos_formula': 'close[T+h]/close[T]-1',
        'agreement_comparable': False,
        'agreement_reason': 'horizon_mismatch' if record.get('model_horizon') != horizon else 'label_target_mismatch',
        'a_share_new_inventory_executable': horizon > 1,
        'payoff_kind': 'descriptive_intraday' if horizon == 1 else 'descriptive_open_to_close',
        'factor_direction': 'relative_to_cross_section_median',
        'price_accuracy_available': False,
    }
    return row


def aggregate(rows, buckets):
    values = [row['rank_ic'] for row in rows if row.get('rank_ic') is not None]
    agreement = [row['sign_agree'] for row in rows if row.get('sign_agree') is not None]
    result = dict(days=len(rows), rank_ic_mean=float(np.mean(values)) if values else None,
                  ic_ir=None, sign_agree_mean=float(np.mean(agreement)) if agreement else None,
                  coverage_mean=float(np.mean([row['coverage'] for row in rows])) if rows else None)
    if len(values) > 1 and np.std(values, ddof=1) > 0:
        result['ic_ir'] = float(np.mean(values) / np.std(values, ddof=1))
    rolling = {'buckets': buckets(rows), 'window_basis': 'last_recorded_signal_days',
               'group': rows[-1]['group'] if rows else None}
    for window in (5, 20, 60):
        subset = rows[-window:]
        ic = [row['rank_ic'] for row in subset if row.get('rank_ic') is not None]
        agree = [row['sign_agree'] for row in subset if row.get('sign_agree') is not None]
        rolling[f'last{window}'] = dict(days=len(subset), requested_days=window,
            rank_ic_mean=float(np.mean(ic)) if ic else None,
            sign_agree_mean=float(np.mean(agree)) if agree else None,
            coverage_mean=float(np.mean([row['coverage'] for row in subset])) if subset else None)
    result['rolling'] = rolling
    return result
