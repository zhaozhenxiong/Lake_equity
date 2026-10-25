"""Bounded, resumable Kronos observations alongside frozen factor selection."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

import numpy as np
import pandas as pd

from .dataset import DatasetSession, RAW_SOURCES, _read, _valid_bars
from .execution import _state
from .models import check_stop
from .registry import canonical, digest
from .workflow import LearningWorkflow


RULE_VERSION = 'factor_rank_kronos_filter_v1'
KRONOS_DEFAULTS = dict(lookback=128, horizon=5, samples=8, seed=42, device='cuda', temperature=.8, top_p=.9)
IMPLEMENTATION_SHA256 = digest(Path(__file__))


def _now():
    return datetime.now().astimezone().isoformat()


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:60\d{4}\.SH|688\d{3}\.SH|00\d{4}\.SZ|30\d{4}\.SZ)', value):
        raise ValueError('Require a Shanghai/Shenzhen A-share symbol')
    return value


class FusionService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'var/learning/fusion'
        self.index = self.root / 'var/learning/fusion.db'

    def list_runs(self, limit=100):
        if not self.index.is_file():
            return {'reports': []}
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():
                return {'reports': []}
            rows = conn.execute('SELECT summary FROM runs ORDER BY rowid DESC LIMIT ?', (max(1, min(200, int(limit))),))
            return {'reports': [json.loads(row[0]) for row in rows]}

    def read_run(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'fusion-[0-9a-f]{32}', run_id):
            raise ValueError('Invalid fusion run identity')
        if not self.index.is_file():
            raise KeyError(run_id)
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT path,sha256 FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        path = self.directory / run_id / row[0]
        if path.is_symlink() or path.parent.is_symlink() or digest(path) != row[1]:
            raise ValueError('Fusion report SHA256 mismatch')
        return json.loads(path.read_text(encoding='utf-8'))

    def _save(self, report):
        folder = self.directory / report['run_id']
        folder.mkdir(parents=True, exist_ok=True)
        # Every continuation is a new immutable observation, never an overwritten report.
        path = folder / ('attempt-' + uuid.uuid4().hex + '.json')
        path.write_text(canonical(report), encoding='utf-8')
        summary = {key: report[key] for key in ('run_id', 'kind', 'family', 'status', 'as_of', 'created_at', 'coverage')}
        summary.update(id=report['run_id'], title='模型与 Kronos 复核', release_id=report.get('release_id'))
        with closing(sqlite3.connect(self.index, timeout=30)) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL,summary TEXT NOT NULL)')
            conn.execute('INSERT INTO runs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,summary=excluded.summary',
                         (report['run_id'], path.name, digest(path), canonical(summary)))
            conn.commit()
        return report

    def _market_inputs(self, scan, symbols, lookback):
        session = DatasetSession(self.root, scan['read_ref'])
        sources = {}
        as_of = scan['as_of']
        through = (date.fromisoformat(as_of) + timedelta(days=45)).isoformat()
        calendar = _read(session.store, session.snapshot, 'trading_calendar', '1900-01-01', through,
                         None, sources, None, session)
        days = sorted(pd.to_datetime(calendar.loc[calendar.is_trading.eq(True), 'trade_date']).dt.strftime('%Y-%m-%d').tolist())
        if len(set(days)) != len(days) or as_of not in days:
            raise ValueError('Fusion requires a unique verified signal session')
        following = [day for day in days if day > as_of]
        bars = _read(session.store, session.snapshot, 'daily_bars', '1900-01-01', as_of,
                     symbols, sources, None, session)
        bars = bars.rename(columns={'trade_date': 'date'})
        bars['date'] = pd.to_datetime(bars.date).dt.strftime('%Y-%m-%d')
        if bars.duplicated(['date', 'symbol']).any():
            raise ValueError('Duplicate frozen prediction bars')
        frames, quotes = {}, {}
        for symbol in symbols:
            frame = bars.loc[bars.symbol.eq(symbol)].sort_values('date').tail(max(lookback, 21)).copy()
            frame.attrs.update(price_basis='unadjusted', volume_unit='shares', amount_unit='CNY', snapshot_read_ref=scan['read_ref'])
            expected = [day for day in days if day <= as_of][-lookback:]
            frame.attrs['calendar_history_complete'] = len(expected) == lookback and frame.date.tail(lookback).tolist() == expected
            if 'has_trade_price' not in frame:
                # The lake materialization keeps legacy has_trade_price semantics;
                # qualify by audited source instead (see the 2026-10-24 THS audit).
                frame['has_trade_price'] = frame.source.astype(str).isin(RAW_SOURCES).astype(int)
            frames[symbol] = frame
            exact = frame.loc[frame.date.eq(as_of)]
            quote = dict(reference_close=None, prior_volume=None, atr_pct=None, reference_date=as_of,
                         quote_usable=False, quote_reason='missing_signal_session_bar')
            if len(exact) == 1:
                row = exact.iloc[0]
                observed = row.to_dict()
                flagged = any(_state(observed, key) is True for key in
                              ('corporate_action_suspected', 'wyckoff_corporate_action_suspected', 'suspended', 'is_suspended'))
                declared_invalid = any(_state(observed, key) is False for key in ('bar_valid', 'has_trade_price'))
                valid = bool(_valid_bars(exact).iloc[0]) and not flagged and not declared_invalid
                quote.update(quote_usable=valid, quote_reason=None if valid else 'invalid_unverified_or_corporate_action_bar')
                if valid:
                    quote['reference_close'] = float(row.close)
                    quote['prior_volume'] = float(row.volume)
                close = frame.close.shift(1)
                true_range = pd.concat([frame.high - frame.low, (frame.high - close).abs(), (frame.low - close).abs()], axis=1).max(axis=1)
                atr = float(true_range.tail(20).mean() / row.close) if row.close > 0 else np.nan
                quote['atr_pct'] = atr if valid and np.isfinite(atr) else None
                quote['source'] = str(row.source)
            quotes[symbol] = quote
        session.verify()
        return frames, quotes, following, dict(read_ref=scan['read_ref'], sources=sources,
                                               calendar_recent=[day for day in days if day <= as_of][-64:])

    def _forecast(self, frame, future, symbol, as_of, parameters, cancel):
        from my_strategy.services.kronos_prediction import predict, _source_identity
        from my_strategy.services.kronos_assets import asset_status
        if frame.attrs.get('calendar_history_complete') is not True:
            raise ValueError('Kronos lookback has missing verified trading sessions')
        assets = asset_status()
        if not assets['ready']:
            raise FileNotFoundError('Pinned Kronos assets are unavailable')
        key = hashlib.sha256(canonical(dict(symbol=symbol, as_of=as_of, future=future, parameters=parameters,
                 data=frame.to_json(orient='split', double_precision=15), contract=frame.attrs,
                 calendar=frame.attrs['snapshot_read_ref'], assets=assets['files'], source=_source_identity())).encode()).hexdigest()
        if self.index.is_file():
            with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='predictions'").fetchone()
                saved = conn.execute('SELECT path,sha256 FROM predictions WHERE id=?', (key,)).fetchone() if exists else None
            if saved:
                path = self.directory / 'predictions' / saved[0]
                if path.is_symlink() or digest(path) != saved[1]:
                    raise ValueError('Cached Kronos observation SHA256 mismatch')
                result = json.loads(path.read_text(encoding='utf-8'))
                result.setdefault('resource', {})['reused_observation'] = True
                result['resource']['fresh_inference'] = False
                result['resource']['execution_observation_scope'] = 'original_cached_generation'
                return result
        result = predict(frame, future, symbol=symbol, as_of=as_of,
                         calendar_identity={'read_ref': frame.attrs['snapshot_read_ref']}, calendar_verified=True,
                         cancel_check=cancel, **parameters)
        folder = self.directory / 'predictions'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (key + '-' + uuid.uuid4().hex + '.json')
        path.write_text(canonical(result), encoding='utf-8')
        with closing(sqlite3.connect(self.index, timeout=30)) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS predictions(id TEXT PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO predictions VALUES(?,?,?)', (key, path.name, digest(path)))
            conn.commit()
        return result

    def run(self, params, progress=None, check_cancel=None):
        from .releases import ReleaseRegistry
        from lake_equity.jobs import JobCancelled
        allowed = {'release_id', 'model_id', 'scan_id', 'end', 'scope', 'symbols', 'read_ref',
                   'candidate_count', 'holding_symbols', 'kronos', 'max_calls', 'budget_seconds', 'resume_id', 'mode'}
        if set(params) - allowed:
            raise ValueError('Unknown fusion parameters: ' + ', '.join(sorted(set(params) - allowed)))
        old = self.read_run(params['resume_id']) if params.get('resume_id') else None
        supplied = {**(old['request'] if old else {}), **params}
        supplied.pop('resume_id', None)
        release = ReleaseRegistry(self.root).resolve(supplied)
        if release is None:
            raise ValueError('Fusion requires a frozen strategy release, not an unbound model')
        mode = supplied.get('mode', (release.get('fusion') or {}).get('mode', 'factor_baseline'))
        if mode not in {'factor_baseline', 'kronos_filter'}:
            raise ValueError('Fusion mode must be factor_baseline or kronos_filter')
        count = supplied.get('candidate_count', (release.get('fusion') or {}).get('candidate_count', 200))
        max_calls, budget = supplied.get('max_calls', 20), supplied.get('budget_seconds', 300)
        for name, value, maximum in [('candidate_count', count, 200), ('max_calls', max_calls, 200)]:
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(name + ' must be an integer in [1,' + str(maximum) + ']')
        if isinstance(budget, bool) or not np.isfinite(float(budget)) or not 0 < float(budget) <= 1800:
            raise ValueError('budget_seconds must be in (0,1800]')
        kronos = {**KRONOS_DEFAULTS, **supplied.get('kronos', {})}
        if set(kronos) - set(KRONOS_DEFAULTS):
            raise ValueError('Unknown Kronos parameters')
        for key, lower, upper in [('lookback', 16, 512), ('horizon', 1, 10), ('samples', 1, 64), ('seed', 0, 2**63 - 1)]:
            if isinstance(kronos[key], bool) or not isinstance(kronos[key], int) or not lower <= kronos[key] <= upper:
                raise ValueError('Invalid Kronos ' + key)
        for key, maximum in [('temperature', 2), ('top_p', 1)]:
            value = kronos[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
                raise ValueError('Invalid Kronos ' + key)
        held = sorted({_symbol(value) for value in supplied.get('holding_symbols', [])})
        workflow = LearningWorkflow(self.root)
        if old:
            scan = workflow.read_run(old['scan_id'])
        elif supplied.get('scan_id'):
            scan = workflow.read_run(supplied['scan_id'])
        else:
            scan = workflow.scan({key: value for key, value in dict(release_id=release['release_id'], model_id=release['model_id'], end=supplied.get('end'),
                                 scope=supplied.get('scope', 'all'), symbols=supplied.get('symbols'), read_ref=supplied.get('read_ref'), top_k=100).items() if value is not None},
                                 progress=progress, check_cancel=check_cancel)
        if scan['model_id'] != release['model_id'] or scan.get('kind') != 'scan':
            raise ValueError('Fusion scan and frozen release model disagree')
        if not scan.get('final_ranking') or scan['status'] != 'completed':
            raise ValueError('Fusion requires a complete baseline ranking')
        expected_policy = (release.get('universe') or {}).get('policy')
        if expected_policy:
            from .universe import read_universe
            if not scan.get('universe'):
                raise ValueError('Baseline scan has no frozen release universe evidence')
            observed_universe = read_universe(scan['universe'])
            if (observed_universe['policy'] != expected_policy or observed_universe['read_ref'] != scan['read_ref']
                    or not observed_universe['start'] <= scan['as_of'] <= observed_universe['end']):
                raise ValueError('Baseline scan differs from frozen release universe policy, data or date')
        if supplied.get('read_ref') and supplied['read_ref'] != scan['read_ref']:
            raise ValueError('Fusion read_ref differs from baseline scan')
        if supplied.get('end') and supplied['end'] != scan.get('requested_cutoff', scan['as_of']):
            raise ValueError('Fusion requested cutoff differs from baseline scan')
        contract = dict(release_id=release.get('release_id'), model_id=release['model_id'], scan_id=scan['run_id'],
                        read_ref=scan['read_ref'], mode=mode, candidate_count=count, holding_symbols=held, kronos=kronos, rule_version=RULE_VERSION)
        if old and contract != old['contract']:
            raise ValueError('Resume must retain the original frozen fusion contract')
        selected = [row for row in scan['rows'] if row.get('rank') is not None][:count]
        symbols = list(dict.fromkeys([row['symbol'] for row in selected] + held))
        frames, quotes, future, evidence = self._market_inputs(scan, symbols, int(kronos['lookback']))
        if len(future) < int(kronos['horizon']):
            raise ValueError('Insufficient verified future trading calendar')
        by_symbol = {row['symbol']: row for row in scan['rows']}
        previous = {row['symbol']: row for row in old['rows']} if old else {}
        rows = []
        for symbol in symbols:
            baseline = by_symbol.get(symbol, {})
            row = dict(symbol=symbol, factor_score=baseline.get('score'), factor_rank=baseline.get('rank'),
                       baseline_status=baseline.get('status', 'not_in_baseline'), is_candidate=symbol in {r['symbol'] for r in selected},
                       is_holding=symbol in held, kronos_status='deferred', kronos=None, prediction_id=None, **quotes[symbol])
            if symbol in previous:
                row = previous[symbol]
            rows.append(row)
        started, calls, cancelled = time.monotonic(), 0, False
        try:
            for row in sorted(rows, key=lambda row: (not row['is_holding'], row['factor_rank'] or 10**9, row['symbol'])):
                check_stop(check_cancel)
                if row['kronos_status'] != 'deferred':
                    continue
                if calls >= max_calls or time.monotonic() - started >= float(budget):
                    break
                calls += 1
                try:
                    forecast = self._forecast(frames[row['symbol']], future[:int(kronos['horizon'])], row['symbol'], scan['as_of'], kronos, check_cancel)
                    usable = (forecast.get('quality') or {}).get('prediction_usable') is True
                    summary = dict(forecast.get('summary') or {})
                    bars = forecast.get('forecast') or []
                    # This is only a scenario ratio, not an expected executable return.
                    if bars and bars[0].get('open', 0) > 0:
                        summary['median_path_close_h_to_open_1_return'] = bars[-1]['close'] / bars[0]['open'] - 1
                    row.update(kronos_status='succeeded' if usable else 'unusable', prediction_id=forecast.get('cache_key'),
                               kronos=dict(summary=summary, quality=forecast.get('quality'), identity=forecast.get('identity'),
                                           resource=forecast.get('resource'), scenario_return_semantics='ratio_of_marginal_medians_not_executable_expected_return'))
                except (JobCancelled, InterruptedError):
                    row['kronos_status'] = 'deferred'
                    cancelled = True
                    break
                except (ValueError, OSError, RuntimeError) as error:
                    row.update(kronos_status='failed', reason=f'{type(error).__name__}: {error}')
                if progress:
                    progress(dict(phase='kronos_candidates', current=calls, total=min(max_calls, len(rows)), symbol=row['symbol']))
        except JobCancelled:
            cancelled = True
        if cancelled:
            from my_strategy.services.kronos_prediction import release_prediction_resources
            release_prediction_resources()
        accepted = []
        for row in rows:
            summary = (row.get('kronos') or {}).get('summary') or {}
            quality = (row.get('kronos') or {}).get('quality') or {}
            if row['kronos_status'] == 'unusable':
                reasons = []
                if quality.get('valid_samples', 0) < quality.get('minimum_valid_samples', 8):
                    reasons.append('insufficient_legal_sample_paths')
                if quality.get('valid_fraction', 0) < quality.get('minimum_valid_fraction', .8):
                    reasons.append('legal_sample_fraction_below_threshold')
                if not quality.get('input_contract_verified'):
                    reasons.append('input_contract_unverified')
                if not quality.get('calendar_verified'):
                    reasons.append('calendar_unverified')
                row['reason'] = ','.join(reasons) or 'prediction_quality_unusable'
            positive = (row['kronos_status'] == 'succeeded' and summary.get('terminal_return_median', -1) > 0
                        and summary.get('terminal_return_p10', -1) >= -.10)
            row['kronos_filter_passed'] = positive
            row['selection_eligible'] = (row['is_candidate'] and row.get('quote_usable', bool(row.get('reference_close')))
                                         and (mode == 'factor_baseline' or positive))
            row['direction'] = 'BUY' if row['selection_eligible'] else 'WAIT'
            row['fused_rank'] = None
            if row['selection_eligible']:
                accepted.append(row)
        accepted.sort(key=lambda row: (row['factor_rank'], row['symbol']))
        for rank, row in enumerate(accepted, 1):
            row['fused_rank'] = rank
        coverage = {status: sum(row['kronos_status'] == status for row in rows) for status in ('succeeded', 'unusable', 'failed', 'deferred')}
        complete = not coverage['deferred'] and not cancelled
        requested = dict(supplied, release_id=release.get('release_id'), model_id=release['model_id'], scan_id=scan['run_id'], kronos=kronos,
                         candidate_count=count, holding_symbols=held, mode=mode)
        report = dict(schema='frozen_fusion_scan_v1', family='fusion_scan_v1', kind='fusion', run_id=old['run_id'] if old else 'fusion-' + uuid.uuid4().hex,
                      created_at=old['created_at'] if old else _now(), updated_at=_now(), status='cancelled' if cancelled else 'completed' if complete else 'partial',
                      request=requested, contract=contract, release_id=release.get('release_id'), release_identity=release,
                      model_id=release['model_id'], scan_id=scan['run_id'], read_ref=scan['read_ref'], as_of=scan['as_of'], next_session=future[0],
                      mode=mode, rule_version=RULE_VERSION, baseline_rows=scan['rows'], rows=rows,
                      implementation_sha256=IMPLEMENTATION_SHA256,
                      coverage=dict(total=len(rows), baseline_total=len(scan['rows']), candidates=len(selected), holdings=len(held), **coverage),
                      final_ranking=complete if mode == 'kronos_filter' else True,
                      calls_this_attempt=calls, total_calls=(old.get('total_calls', 0) if old else 0) + calls,
                      elapsed_seconds=time.monotonic() - started, market_evidence=evidence,
                      qualification=dict(research_only=True, strict_history_eligible=False, paper_allocation_eligible=False,
                                         kronos_pretraining_cutoff_verified=False, kronos_release_date_verified=False,
                                         historical_fusion_semantics='reconstruction_with_unverified_pretraining_boundary',
                                         policy_validation='factor_baseline_lambda_zero' if mode == 'factor_baseline' else 'unvalidated_fixed_kronos_filter'),
                      limitations=['Kronos sample frequency is not calibrated probability.',
                                   'Forecast close-to-close returns and ratios of marginal medians are not executable profit.',
                                   'Default factor_baseline gives Kronos zero selection weight; filters require explicit mode.',
                                   'Deferred, failed and unusable predictions remain visible; no substitute forecast is fabricated.'])
        return self._save(report)
