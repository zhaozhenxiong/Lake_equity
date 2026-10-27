"""Bounded historical Kronos experiments, separate from the live forward ledger."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from filelock import FileLock
import numpy as np
import pandas as pd
import polars as pl

from lake_equity.jobs import JobCancelled
from lake_equity.market.store import _inside
from .dataset import BAR_COLUMNS, DatasetSession, RAW_SOURCES, _read, _valid_bars
from .device import resolve as resolve_device
from .execution import _state
from .fusion import FusionService
from .models import check_stop
from .registry import ModelRegistry, canonical, digest
from .verification import prediction_agreement
from .workflow import LearningWorkflow


SCHEMA = 'kronos_replay_v1'
PARAMETERS = {'days', 'symbols', 'end', 'read_ref', 'include_latest', 'horizon', 'lookback', 'samples',
              'seed', 'device', 'temperature', 'top_p', 'series_id', 'top_k', 'max_calls',
              'budget_seconds', 'resume_id', 'evaluation_read_ref', 'expected_experiment_id'}
KRONOS_KEYS = ('lookback', 'horizon', 'samples', 'seed', 'device', 'temperature', 'top_p')
QUALIFICATION = dict(research_only=True, historical_replay=True, live_eligible=False,
                     strict_history_eligible=False, paper_allocation_eligible=False,
                     kronos_pretraining_cutoff_verified=False, trading_performance=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _number(value):
    try:
        return float(value) if value is not None and np.isfinite(float(value)) else None
    except (TypeError, ValueError):
        return None


def _factor_reason(model, member, day, horizon, read_ref):
    if not member['start'] <= day <= member['end']:
        return 'outside_saved_fold'
    if str(model.get('information_cutoff', '9999'))[:10] >= day:
        return 'information_cutoff_not_before_signal'
    if model.get('horizon') != horizon:
        return 'horizon_mismatch'
    formula = str(model.get('label_formula') or '').split(';')[0].replace(' ', '')
    if formula != 'close[T+horizon]/close[T]-1':
        return 'label_target_mismatch'
    if model.get('training_read_ref') != read_ref:
        return 'data_snapshot_mismatch'
    return None


class KronosReplayService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'var/learning/kronos_replays'
        self.index = self.root / 'var/learning/kronos_replays.db'

    def _normalize(self, params):
        if not isinstance(params, dict) or set(params) - PARAMETERS:
            raise ValueError('Unknown Kronos replay parameters')
        value = dict(days=20, horizon=1, lookback=128, samples=8, seed=42, device=None,
                     temperature=.8, top_p=.9, top_k=2, include_latest=False,
                     max_calls=200, budget_seconds=300.)
        value.update(params)
        for key, lower, upper in [('lookback', 16, 512), ('samples', 8, 64), ('seed', 0, 2**63-1),
                                  ('top_k', 1, 100), ('max_calls', 1, 6000)]:
            if isinstance(value[key], bool) or not isinstance(value[key], int) or not lower <= value[key] <= upper:
                raise ValueError(f'{key} must be an integer in [{lower},{upper}]')
        if type(value['days']) is not int or value['days'] not in (2, 20, 30):
            raise ValueError('days must be 20 or 30 (2 is reserved for a bounded pilot)')
        if type(value['horizon']) is not int or value['horizon'] not in (1, 5):
            raise ValueError('horizon must be 1 or 5')
        if type(value['include_latest']) is not bool:
            raise ValueError('include_latest must be boolean')
        for key, maximum in [('temperature', 2), ('top_p', 1), ('budget_seconds', 1800)]:
            if (isinstance(value[key], bool) or not isinstance(value[key], (int, float))
                    or _number(value[key]) is None or not 0 < value[key] <= maximum):
                raise ValueError(f'{key} must be in (0,{maximum}]')
        symbols = value.get('symbols')
        if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 200
                or any(not isinstance(symbol, str) or not re.fullmatch(r'(?:60\d{4}\.SH|688\d{3}\.SH|00\d{4}\.SZ|30\d{4}\.SZ)', symbol) for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError('symbols must contain 1–200 unique Shanghai/Shenzhen A shares')
        value['symbols'] = sorted(symbols)
        value['device'] = resolve_device(value['device'])
        if value.get('end'):
            if not isinstance(value['end'], str) or date.fromisoformat(value['end']).isoformat() != value['end']:
                raise ValueError('end must be an ISO date')
        for key, prefix, length in [('read_ref', '', 64), ('evaluation_read_ref', '', 64),
                                    ('series_id', 'series-', 32), ('resume_id', 'replay-', 32),
                                    ('expected_experiment_id', 'replay-', 32)]:
            if value.get(key) and (not isinstance(value[key], str)
                                   or not re.fullmatch(prefix + '[0-9a-f]{' + str(length) + '}', value[key])):
                raise ValueError('Invalid ' + key)
        return value

    def _assets(self):
        from my_strategy.services.kronos_assets import asset_status
        from my_strategy.services.kronos_prediction import _source_identity
        assets = asset_status()
        return dict(ready=assets['ready'], files=assets.get('files', []),
                    source=_source_identity() if assets['ready'] else None,
                    missing=assets.get('missing'), errors=assets.get('errors'))

    def _factor_rows(self, values, signal_days):
        result = {(day, symbol): dict(score=None, rank=None, model_id=None, reason='no_saved_series')
                  for day in signal_days for symbol in values['symbols']}
        registry = ModelRegistry(self.root)
        try:
            series_id = values.get('series_id')
            if not series_id:
                from .releases import ReleaseRegistry
                series_id = (ReleaseRegistry(self.root).active() or {}).get('series_id')
            if not series_id:
                return result, None
            values['series_id'] = series_id
            series = registry.read_series(series_id)
            records, models = None, {}
            identity = dict(series_id=series_id, version=series['version'], predictions_sha256=None, models={})
            for day in signal_days:
                members = [member for member in series['members'] if member['start'] <= day <= member['end']]
                if len(members) != 1:
                    for symbol in values['symbols']:
                        result[(day, symbol)]['reason'] = 'outside_or_overlapping_saved_folds'
                    continue
                member = members[0]
                if member['model_id'] not in models:
                    models[member['model_id']] = registry.read_model(member['model_id'])
                model = models[member['model_id']]
                identity['models'][model['model_id']] = model['version']
                reason = _factor_reason(model, member, day, values['horizon'], values['read_ref'])
                if reason is None and records is None:
                    from .service import LearningService
                    service = LearningService(self.root)
                    saved = service.read_run(series['run_id'])
                    path = service.runs_root / series['run_id'] / 'predictions.parquet'
                    expected = saved['artifacts']['predictions.parquet']['sha256']
                    if path.is_symlink() or digest(path) != expected:
                        raise ValueError('Saved factor prediction SHA256 mismatch')
                    records = (pl.scan_parquet(path).filter(
                        pl.col('date').is_in(signal_days) & pl.col('symbol').is_in(values['symbols']) &
                        pl.col('model_name').eq(series['model_name'])).collect().to_pandas())
                    if digest(path) != expected:
                        raise ValueError('Saved factor prediction changed during replay preparation')
                    identity['predictions_sha256'] = expected
                for symbol in values['symbols']:
                    row = dict(score=None, rank=None, model_id=model['model_id'], horizon=model['horizon'],
                               information_cutoff=model['information_cutoff'], frozen_at=model['frozen_at'],
                               chronology='historical_reconstruction', reason=reason)
                    if reason is None:
                        selected = records.loc[(records.date.astype(str).str[:10] == day) &
                                               records.symbol.eq(symbol) & records.fold.eq(member['fold']) &
                                               records.model_name.eq(series['model_name'])]
                        if len(selected) == 1 and _number(selected.iloc[0].score) is not None:
                            row['score'] = float(selected.iloc[0].score)
                        else:
                            row['reason'] = 'missing_or_ambiguous_saved_prediction'
                    result[(day, symbol)] = row
                ranked = sorted([symbol for symbol in values['symbols'] if result[(day, symbol)]['score'] is not None],
                                key=lambda symbol: (-result[(day, symbol)]['score'], symbol))
                for rank, symbol in enumerate(ranked, 1):
                    result[(day, symbol)]['rank'] = rank
            return result, identity
        except (ValueError, KeyError, OSError, sqlite3.Error) as error:
            for row in result.values():
                row.update(score=None, rank=None, reason='saved_factor_unavailable', detail=str(error))
            return result, dict(error=str(error))

    def _prepare(self, values, check_cancel=None):
        started = time.monotonic()
        session = DatasetSession(self.root, values.get('read_ref'), check_cancel)
        values['read_ref'] = session.snapshot['read_ref']
        latest = session.snapshot['datasets']['daily_bars'].get('coverage_end')
        if not latest:
            raise ValueError('No published daily bar coverage for replay')
        end = min(values.get('end') or latest, latest)
        through = (date.fromisoformat(end) + timedelta(days=60)).isoformat()
        calendar = LearningWorkflow(self.root)._sessions(session, through, {})
        candidates = [day for index, day in enumerate(calendar) if day <= end and
                      index + values['horizon'] < len(calendar) and
                      (values['include_latest'] or calendar[index + values['horizon']] <= latest)]
        if len(candidates) < values['days']:
            raise ValueError('Not enough verified signal sessions for the requested replay window')
        signal_days = candidates[-values['days']:]
        first = max(0, calendar.index(signal_days[0]) - values['lookback'] + 1)
        sources = {}
        history = _read(session.store, session.snapshot, 'daily_bars', calendar[first], signal_days[-1],
                        values['symbols'], sources, check_cancel, session)
        history = history.rename(columns={'trade_date': 'date'})
        history['date'] = pd.to_datetime(history.date).dt.strftime('%Y-%m-%d')
        if history.duplicated(['date', 'symbol']).any():
            raise ValueError('Duplicate frozen replay bars')
        factors, factor_identity = self._factor_rows(values, signal_days)
        rows, frames = [], {}
        for day in signal_days:
            index = calendar.index(day)
            expected = calendar[max(0, index-values['lookback']+1):index+1]
            for symbol in values['symbols']:
                frame = history.loc[history.symbol.eq(symbol) & history.date.le(day)].sort_values('date').tail(values['lookback']).reset_index(drop=True)
                reason = None
                if frame.empty or frame.date.iloc[-1] != day:
                    reason = 'missing_signal_session_bar'
                elif len(frame) != values['lookback'] or len(expected) != values['lookback']:
                    reason = 'insufficient_history'
                elif frame.date.tolist() != expected:
                    reason = 'missing_verified_history_sessions'
                elif not _valid_bars(frame).all():
                    reason = 'invalid_or_unverified_history_bar'
                if 'has_trade_price' not in frame:
                    frame['has_trade_price'] = frame.source.astype(str).isin(RAW_SOURCES).astype(int)
                frame.attrs.update(price_basis='unadjusted', volume_unit='shares', amount_unit='CNY',
                                   snapshot_read_ref=values['read_ref'], calendar_history_complete=reason is None)
                key = (day, symbol)
                frames[key] = frame
                window_sha = hashlib.sha256(frame[['date', *BAR_COLUMNS]].to_json(orient='split', double_precision=15).encode()).hexdigest()
                rows.append(dict(signal_day=day, target_day=calendar[index+values['horizon']],
                                 future_days=calendar[index+1:index+values['horizon']+1], symbol=symbol,
                                 input_reason=reason, input_sha256=window_sha,
                                 last_close=_number(frame.close.iloc[-1]) if not frame.empty else None,
                                 factor=factors[key], status='deferred', prediction_id=None, forecast=[],
                                 quality=None, resource=None, realized={'status': 'not_evaluated'}, calls=0))
        assets = self._assets()
        session.verify()
        dependencies = {f'{name}:{start}:{finish}': evidence for (name, start, finish), evidence in session.evidence.items()}
        contract = dict(schema=SCHEMA, days=values['days'], signal_days=signal_days, symbols=values['symbols'],
                        end=end, include_latest=values['include_latest'], read_ref=values['read_ref'],
                        revision_map=session.snapshot['revision_map'], kronos={key: values[key] for key in KRONOS_KEYS},
                        series_id=values.get('series_id'), top_k=values['top_k'], assets=assets,
                        factor_identity=factor_identity, sources=dependencies,
                        windows=[dict(signal_day=row['signal_day'], symbol=row['symbol'], input_sha256=row['input_sha256']) for row in rows],
                        implementation={path.name: digest(path) for path in [Path(__file__), Path(__file__).with_name('fusion.py'), Path(__file__).with_name('dataset.py')]})
        return dict(session=session, rows=rows, frames=frames, contract=contract, assets=assets,
                    preparation_seconds=time.monotonic()-started)

    def preview(self, params):
        values = self._normalize(params)
        context = self._prepare(values)
        identity = 'replay-' + _hash(context['contract'])[:32]
        request = {key: value for key, value in values.items() if key not in {'resume_id', 'expected_experiment_id'}}
        request['expected_experiment_id'] = identity
        blockers = [] if context['assets']['ready'] else ['kronos_assets_unavailable']
        return dict(schema='kronos_replay_preview_v1', kind='kronos_replay', status='blocked' if blockers else 'ready',
                    experiment_id=identity, request=request, contract=context['contract'],
                    signal_days=context['contract']['signal_days'], rows=context['rows'], blockers=blockers,
                    coverage=self._coverage(context['rows']), qualification=dict(QUALIFICATION),
                    budget=dict(max_calls=values['max_calls'], budget_seconds=values['budget_seconds'],
                                required_observations=len(context['rows']), preparation_seconds=context['preparation_seconds']))

    def _connect(self):
        _inside(self.directory, self.root / 'var')
        _inside(self.index, self.root / 'var')
        self.directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.index, timeout=30)
        conn.executescript('''
CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, contract TEXT NOT NULL, path TEXT NOT NULL,
 sha256 TEXT NOT NULL, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
 path TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
''')
        return closing(conn)

    def _document(self, experiment_id, category, payload):
        relative = f'{category}/{uuid.uuid4().hex}.json'
        path = _inside(self.directory / experiment_id / relative, self.root / 'var')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical(payload), encoding='utf-8')
        return dict(path=relative, sha256=digest(path))

    def _save(self, report):
        report.pop('artifact_sha256', None)
        report['attempt_id'] = 'attempt-' + uuid.uuid4().hex
        report['updated_at'] = _now()
        report['coverage'] = self._coverage(report['rows'])
        report['summary'] = self._summary(report['rows'], report['contract']['top_k'])
        relative = report['attempt_id'] + '.json'
        path = _inside(self.directory / report['run_id'] / relative, self.root / 'var')
        path.parent.mkdir(parents=True, exist_ok=True)
        report['artifact_path'] = relative
        path.write_text(canonical(report), encoding='utf-8')
        sha = digest(path)
        summary = {key: report[key] for key in ['run_id', 'kind', 'family', 'status', 'created_at', 'updated_at', 'coverage']}
        summary.update(id=report['run_id'], title='Kronos 历史逐日验证', experiment_id=report['run_id'],
                       days=report['contract']['days'], horizon=report['contract']['kronos']['horizon'])
        with self._connect() as conn:
            conn.execute('INSERT INTO attempts VALUES(?,?,?,?,?)', (report['attempt_id'], report['run_id'], relative, sha, report['updated_at']))
            conn.execute('INSERT INTO experiments VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,summary=excluded.summary',
                         (report['run_id'], canonical(report['contract']), relative, sha, canonical(summary)))
            conn.commit()
        report['artifact_sha256'] = sha
        return report

    def list_runs(self, limit=100, offset=0):
        if offset < 0 or (limit is not None and not 1 <= limit <= 1000):
            raise ValueError('Invalid replay pagination')
        if not self.index.is_file():
            return dict(reports=[], total=0, offset=offset, limit=limit, has_more=False)
        path = _inside(self.index, self.root / 'var')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as conn:
            total = conn.execute('SELECT count(*) FROM experiments').fetchone()[0]
            records = conn.execute('SELECT summary FROM experiments ORDER BY rowid DESC LIMIT ? OFFSET ?',
                                   (-1 if limit is None else limit, offset)).fetchall()
        return dict(reports=[json.loads(row[0]) for row in records], total=total, offset=offset,
                    limit=limit, has_more=limit is not None and offset+limit < total)

    def read(self, experiment_id):
        if not isinstance(experiment_id, str) or not re.fullmatch('replay-[0-9a-f]{32}', experiment_id):
            raise ValueError('Invalid replay experiment identity')
        if not self.index.is_file():
            raise KeyError(experiment_id)
        index = _inside(self.index, self.root / 'var')
        with closing(sqlite3.connect(index.as_uri()+'?mode=ro', uri=True)) as conn:
            record = conn.execute('SELECT path,sha256,contract FROM experiments WHERE id=?', (experiment_id,)).fetchone()
        if not record:
            raise KeyError(experiment_id)
        path = _inside(self.directory / experiment_id / record[0], self.directory / experiment_id)
        if digest(path) != record[1]:
            raise ValueError('Replay report SHA256 mismatch')
        report = json.loads(path.read_text(encoding='utf-8'))
        if (report['run_id'] != experiment_id or canonical(report['contract']) != record[2]
                or 'replay-' + _hash(report['contract'])[:32] != experiment_id):
            raise ValueError('Replay frozen contract identity mismatch')
        for row in report['rows']:
            for key in ('observation', 'evaluation'):
                artifact = row.get(key)
                if artifact:
                    path = _inside(self.directory / experiment_id / artifact['path'], self.directory / experiment_id)
                    if digest(path) != artifact['sha256']:
                        raise ValueError('Replay observation/evaluation SHA256 mismatch')
        return dict(report, artifact_sha256=record[1])

    read_run = read

    def resume(self, experiment_id, params=None, progress=None, check_cancel=None):
        changes = dict(params or {})
        if set(changes) - {'max_calls', 'budget_seconds', 'evaluation_read_ref'}:
            raise ValueError('Resume must retain the original frozen replay contract')
        previous = self.read(experiment_id)
        return self.run({**previous['request'], **changes, 'resume_id': experiment_id}, progress, check_cancel)

    def run(self, params, progress=None, check_cancel=None):
        if not isinstance(params, dict):
            raise ValueError('Kronos replay parameters must be an object')
        if params.get('resume_id'):
            previous = self.read(params['resume_id'])
            changes = {key: value for key, value in params.items() if key != 'resume_id'}
            for key, value in changes.items():
                if key not in {'max_calls', 'budget_seconds', 'evaluation_read_ref'} and previous['request'].get(key) != value:
                    raise ValueError('Resume must retain the original frozen replay contract')
            values = self._normalize({**previous['request'], **changes, 'resume_id': params['resume_id']})
        else:
            previous = None
            values = self._normalize(params)
        started = time.monotonic()
        context = self._prepare(values, check_cancel)
        identity = 'replay-' + _hash(context['contract'])[:32]
        if (values.get('expected_experiment_id') and values['expected_experiment_id'] != identity
                or previous is not None and previous['run_id'] != identity):
            raise ValueError('Replay frozen contract changed since preview or original execution')
        if not context['assets']['ready']:
            raise ValueError('Pinned Kronos assets are unavailable')
        with self._connect():
            pass
        lock_path = _inside(self.index.with_suffix('.lock'), self.root / 'var')
        with FileLock(str(lock_path), timeout=0):
            if previous is None:
                try:
                    return self.read(identity)
                except KeyError:
                    pass
            report = previous or dict(schema=SCHEMA, family=SCHEMA, kind='kronos_replay', run_id=identity,
                experiment_id=identity, created_at=_now(), status='running', contract=context['contract'],
                request={key: value for key, value in values.items() if key not in {'resume_id', 'expected_experiment_id'}},
                qualification=dict(QUALIFICATION), rows=context['rows'],
                limitations=['Historical replay, not contemporaneously recorded live evidence.',
                             'Uncalibrated model samples; forecast errors are not executable trading returns.',
                             'Current watchlist selection and revised data do not establish historical PIT.'])
            report['status'] = 'running'
            calls, cancelled = 0, False
            self._save(report)
            fusion = FusionService(self.root)
            try:
                for row in report['rows']:
                    check_stop(check_cancel)
                    if row['status'] not in ('deferred', 'failed'):
                        continue
                    if calls >= values['max_calls'] or time.monotonic()-started >= values['budget_seconds']:
                        break
                    if row['input_reason']:
                        row.update(status='failed', reason=row['input_reason'])
                        self._save(report)
                        continue
                    frame = context['frames'][(row['signal_day'], row['symbol'])]
                    calls += 1
                    row['calls'] += 1
                    try:
                        forecast = fusion._forecast(frame, row['future_days'], row['symbol'], row['signal_day'],
                                                    context['contract']['kronos'], check_cancel)
                        if forecast.get('symbol', row['symbol']) != row['symbol'] or forecast.get('as_of', row['signal_day']) != row['signal_day']:
                            raise ValueError('Kronos observation belongs to another signal')
                        if (forecast.get('quality', {}).get('prediction_usable') is True
                                and ([item.get('date') for item in forecast.get('forecast', [])] != row['future_days']
                                     or any(_number(item.get('close')) is None or item['close'] <= 0 for item in forecast.get('forecast', [])))):
                            raise ValueError('Kronos observation target calendar or prices mismatch')
                        row.update(status='succeeded' if forecast.get('quality', {}).get('prediction_usable') is True else 'unusable',
                                   prediction_id=forecast.get('cache_key'), forecast=forecast.get('forecast') or [],
                                   quality=forecast.get('quality'), resource=forecast.get('resource'), reason=None)
                        row['observation'] = self._document(identity, 'observations', dict(
                            signal_day=row['signal_day'], symbol=row['symbol'], input_sha256=row['input_sha256'],
                            last_close=row['last_close'], forecast=forecast, created_at=_now()))
                    except (JobCancelled, InterruptedError):
                        row['reason'] = 'cancelled_during_inference'
                        raise JobCancelled('Kronos replay cancelled')
                    except Exception as error:
                        row.update(status='failed', reason=f'{type(error).__name__}: {error}')
                        row['observation'] = self._document(identity, 'observations', dict(
                            signal_day=row['signal_day'], symbol=row['symbol'], error=row['reason'], created_at=_now()))
                    self._save(report)
                    check_stop(check_cancel)
                    if progress:
                        progress(dict(phase='kronos_replay', current=sum(item['status'] != 'deferred' for item in report['rows']),
                                      total=len(report['rows']), symbol=row['symbol'], signal_day=row['signal_day']))
                context['session'].verify()
                check_stop(check_cancel)
                self._evaluate(report, values, previous is not None, check_cancel)
            except (JobCancelled, InterruptedError):
                cancelled = True
            except Exception as error:
                report['error'] = f'{type(error).__name__}: {error}'
                report['status'] = 'failed'
            if report['status'] != 'failed':
                report['status'] = 'cancelled' if cancelled else 'partial' if any(row['status'] in ('failed', 'deferred') for row in report['rows']) else 'completed'
            report['budget'] = dict(calls_this_attempt=calls, max_calls=values['max_calls'], budget_seconds=values['budget_seconds'],
                                    elapsed_seconds=time.monotonic()-started, preparation_seconds=context['preparation_seconds'])
            return self._save(report)

    def _evaluate(self, report, values, resumed, check_cancel):
        reference = values.get('evaluation_read_ref') or (None if resumed else report['contract']['read_ref'])
        session = DatasetSession(self.root, reference, check_cancel)
        sources = {}
        end = max(row['target_day'] for row in report['rows'])
        bars = _read(session.store, session.snapshot, 'daily_bars', report['contract']['signal_days'][0], end,
                     report['contract']['symbols'], sources, check_cancel, session)
        bars = bars.rename(columns={'trade_date': 'date'})
        bars['date'] = pd.to_datetime(bars.date).dt.strftime('%Y-%m-%d')
        if bars.duplicated(['symbol', 'date']).any():
            raise ValueError('Duplicate realized replay bars')
        latest = session.snapshot['datasets']['daily_bars'].get('coverage_end') or ''
        calendar = LearningWorkflow(self.root)._sessions(session, end, sources)
        updates = []
        for row in report['rows']:
            check_stop(check_cancel)
            realized = {'status': 'not_evaluated', 'reason': 'prediction_not_usable', 'close': None, 'return': None}
            if row['status'] == 'succeeded':
                actual = bars.loc[bars.symbol.eq(row['symbol']) & bars.date.eq(row['target_day'])]
                point = next((item for item in row['forecast'] if item.get('date') == row['target_day']), None)
                if row['target_day'] > latest:
                    realized.update(status='pending', reason='future_session_not_published')
                elif any(day not in calendar for day in row['future_days']):
                    realized.update(status='unavailable', reason='evaluation_calendar_changed')
                elif len(actual) != 1:
                    realized.update(status='unavailable', reason='missing_target_bar')
                elif not _valid_bars(actual).all() or any(_state(actual.iloc[0].to_dict(), key) is True for key in ('suspended', 'is_suspended', 'corporate_action_suspected')):
                    realized.update(status='unavailable', reason='invalid_suspended_or_unverified_target_bar')
                elif point is None or _number(point.get('close')) is None:
                    realized.update(status='unavailable', reason='missing_forecast_target')
                else:
                    truth, predicted, base = float(actual.iloc[0].close), float(point['close']), row['last_close']
                    value, prediction = truth/base-1, predicted/base-1
                    low, high = _number(point.get('close_p10')), _number(point.get('close_p90'))
                    realized.update(status='verified', reason=None, close=truth, predicted_close=predicted,
                                    price_error=predicted-truth, absolute_price_error=abs(predicted-truth),
                                    predicted_return=prediction, return_error=prediction-value,
                                    direction_hit=bool(np.sign(prediction) == np.sign(value)),
                                    interval_covered=low <= truth <= high if low is not None and high is not None else None)
                    realized['return'] = value
            updates.append((row, realized))
        # Labels are not published as verified until their source bytes pass the final check.
        session.verify()
        for row, realized in updates:
            revision = row.get('evaluation_revision', 0)
            if row.get('evaluation') and row['realized'] != realized:
                revision += 1
            row['realized'] = realized
            row['evaluation'] = self._document(report['run_id'], 'evaluations', dict(
                signal_day=row['signal_day'], symbol=row['symbol'], realized=realized,
                evaluation_read_ref=session.snapshot['read_ref'], revision=revision, sources=sources,
                previous_evaluation=row.get('evaluation'), created_at=_now()))
            row['evaluation_revision'] = revision
        report['evaluation_read_ref'] = session.snapshot['read_ref']

    @staticmethod
    def _coverage(rows):
        result = {status: sum(row['status'] == status for row in rows) for status in ('succeeded', 'unusable', 'failed', 'deferred')}
        result.update(requested=len(rows), attempted=sum(row.get('calls', 0) > 0 for row in rows),
                      total_attempts=sum(row.get('calls', 0) for row in rows),
                      cache_hits=sum(bool((row.get('resource') or {}).get('reused_observation')) for row in rows),
                      fresh_inferences=sum(row['status'] in ('succeeded', 'unusable') and not (row.get('resource') or {}).get('reused_observation', False) for row in rows),
                      pending_realized=sum(row['realized']['status'] == 'pending' for row in rows),
                      verified=sum(row['realized']['status'] == 'verified' for row in rows),
                      usable=sum(row['status'] == 'succeeded' for row in rows))
        return result

    @staticmethod
    def _summary(rows, top_k):
        verified = [row for row in rows if row['realized']['status'] == 'verified']
        by_day = []
        for day in sorted({row['signal_day'] for row in rows}):
            paired = [dict(symbol=row['symbol'], factor_score=row['factor']['score'],
                           kronos_median=(next((item['close'] for item in row['forecast'] if item.get('date') == row['target_day']), row['last_close']) / row['last_close'] - 1))
                      for row in rows if row['signal_day'] == day and row['status'] == 'succeeded' and row['factor']['score'] is not None]
            agreement = prediction_agreement(paired, top_k=top_k)
            if len(paired) <= top_k:
                agreement['topk_overlap'] = None
            by_day.append(dict(signal_day=day, **agreement))
        return dict(verified_observations=len(verified),
                    mean_absolute_price_error=float(np.mean([row['realized']['absolute_price_error'] for row in verified])) if verified else None,
                    mean_absolute_return_error=float(np.mean([abs(row['realized']['return_error']) for row in verified])) if verified else None,
                    return_rmse=float(np.sqrt(np.mean([row['realized']['return_error']**2 for row in verified]))) if verified else None,
                    interval_coverage=float(np.mean([row['realized']['interval_covered'] for row in verified if row['realized']['interval_covered'] is not None]))
                    if any(row['realized']['interval_covered'] is not None for row in verified) else None,
                    direction_hit_rate=float(np.mean([row['realized']['direction_hit'] for row in verified])) if verified else None,
                    signal_days=len({row['signal_day'] for row in rows}),
                    same_target_factor_observations=sum(row['factor']['score'] is not None for row in rows), agreement=by_day,
                    target='close[T+horizon]/close[T]-1', trading_return=False)
