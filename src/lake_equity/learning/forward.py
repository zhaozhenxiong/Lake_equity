"""Append-only forward signal ledger and daily reconciliation.

A *forward* record is written the evening a session closes, using only data
published up to that session. Reconciliation happens only after later sessions
are published, and a later data revision never overwrites an earlier row: the
ledger keeps every attempt, every verification and every realised revision.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from . import verification
from .dataset import BAR_COLUMNS, DatasetSession, _read, build_dataset
from .device import report as device_report, resolve as resolve_device
from .models import check_stop, load_saved
from .registry import ModelRegistry, canonical, digest
from .releases import ReleaseRegistry
from .workflow import LearningWorkflow

RULE_VERSION = 'forward_ledger_v1'
AGREEMENT_SIGN = .6
TOPK_OVERLAP = .5
WATCHLIST_LIMIT = 200
KRONOS_KEYS = {'lookback', 'horizon', 'samples', 'seed', 'device', 'temperature', 'top_p'}
ORIGINS = ('live', 'backfill')
RECORD_ALLOWED = {'day', 'release_id', 'symbols', 'horizon', 'device', 'max_calls', 'budget_seconds', 'read_ref',
                  'top_k', 'origin', 'source', 'fusion_run_id'}
VERIFY_ALLOWED = {'day', 'read_ref'}
SYMBOL = re.compile(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)')


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


def _symbols(value):
    if not isinstance(value, list) or not 1 <= len(value) <= WATCHLIST_LIMIT or len(set(value)) != len(value):
        raise ValueError(f'Forward symbols must be 1..{WATCHLIST_LIMIT} unique Shanghai/Shenzhen A-share identities')
    if any(not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) for symbol in value):
        raise ValueError('Only explicit Shanghai/Shenzhen A-share symbols are supported')
    return list(value)


def _number(value):
    try:
        result = float(value)
        return result if np.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _validate_origin(day, latest, origin):
    """A live record must target the newest published session; a backfill must not."""
    if origin not in ORIGINS:
        raise ValueError('origin must be live or backfill')
    if latest is None:
        raise ValueError('No published daily bars are available for the requested cutoff')
    if origin == 'live':
        if latest != day:
            raise ValueError(f'Forward records must target the newest published session; latest is {latest}')
        return
    if day >= latest:
        raise ValueError('Backfill must target a session earlier than the newest published session')


def _agreement_gate(agreement):
    if agreement.get('sign_agree') is None or agreement.get('topk_overlap') is None:
        return dict(status='pending', reason='insufficient_paired_predictions')
    if agreement['sign_agree'] >= AGREEMENT_SIGN and agreement['topk_overlap'] >= TOPK_OVERLAP:
        return dict(status='pass', reason='agreement_thresholds_met')
    return dict(status='fail', reason='agreement_thresholds_not_met')


def _fusion_contract(run):
    """Validate a saved fusion scan and return only the identity needed downstream."""
    if not isinstance(run, dict) or run.get('schema') != 'frozen_fusion_scan_v1':
        raise ValueError('Not a frozen fusion scan report')
    if run.get('kind') != 'fusion' or run.get('status') not in ('completed', 'partial'):
        raise ValueError('Fusion run must be completed or partial')
    day = _day(run.get('as_of'), 'fusion as_of')
    request = run.get('request') or {}
    kronos = request.get('kronos') or {}
    horizon = kronos.get('horizon')
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 20:
        raise ValueError('Fusion run must pin an integer Kronos horizon in [1,20]')
    release = run.get('release_identity') or {}
    release_id, model_id, read_ref = run.get('release_id'), run.get('model_id'), run.get('read_ref')
    if not release_id or not model_id or not read_ref:
        raise ValueError('Fusion run lacks release, model or data identity')
    cutoff = str(release.get('information_cutoff') or '')[:10]
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', cutoff):
        raise ValueError('Fusion run lacks a release information cutoff')
    rows = [row for row in (run.get('rows') or [])
            if row.get('is_candidate') and row.get('factor_score') is not None and row.get('factor_rank') is not None]
    if not rows:
        raise ValueError('Fusion run contains no ranked candidate rows')
    rows = rows[:WATCHLIST_LIMIT]
    return dict(day=day, horizon=horizon, kronos=kronos, release=release, release_id=release_id,
                model_id=model_id, read_ref=read_ref, information_cutoff=cutoff, rows=rows)


class ForwardLedger:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'var/learning/forward'
        self.index = self.root / 'var/learning/forward.db'
        self.registry = ModelRegistry(self.root)
        self.workflow = LearningWorkflow(self.root)

    # -- storage -----------------------------------------------------------
    def _connect(self):
        if self.directory.is_symlink():
            raise ValueError('Forward directory cannot be a symlink')
        self.directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.index, timeout=30)
        conn.executescript('''
CREATE TABLE IF NOT EXISTS days (
 day TEXT PRIMARY KEY, status TEXT NOT NULL, identity TEXT NOT NULL, read_ref TEXT NOT NULL,
 release_id TEXT NOT NULL, model_id TEXT NOT NULL, series_id TEXT, information_cutoff TEXT,
 horizon INTEGER NOT NULL, scope TEXT NOT NULL, symbols_json TEXT NOT NULL, bars_sha256 TEXT,
 artifact_path TEXT, artifact_sha256 TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 origin TEXT NOT NULL DEFAULT 'live');
CREATE TABLE IF NOT EXISTS attempts (
 day TEXT NOT NULL, identity TEXT NOT NULL, artifact_path TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(day, identity));
CREATE TABLE IF NOT EXISTS signals (
 day TEXT NOT NULL, symbol TEXT NOT NULL, source TEXT NOT NULL, model_id TEXT,
 score REAL, rank INTEGER, direction TEXT, expected_return REAL, quality_json TEXT,
 payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(day, symbol, source));
CREATE TABLE IF NOT EXISTS kronos (
 day TEXT NOT NULL, symbol TEXT NOT NULL, prediction_id TEXT, status TEXT NOT NULL,
 quality_json TEXT, summary_json TEXT, params_sha256 TEXT, resource_json TEXT, reused INTEGER,
 created_at TEXT NOT NULL, PRIMARY KEY(day, symbol));
CREATE TABLE IF NOT EXISTS verifications (
 verification_id TEXT PRIMARY KEY, day TEXT NOT NULL, horizon INTEGER NOT NULL,
 source_read_ref TEXT NOT NULL, revision INTEGER NOT NULL, artifact_path TEXT, artifact_sha256 TEXT,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS realized (
 verification_id TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL, horizon INTEGER NOT NULL,
 exec_day TEXT, exit_day TEXT, exec_open REAL, exit_close REAL, ret_gross REAL, ret_net REAL, ret_net_2x REAL,
 executable INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(verification_id, symbol));
CREATE TABLE IF NOT EXISTS deviations (
 verification_id TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL, horizon INTEGER NOT NULL,
 predicted REAL, predicted_rank INTEGER, realized REAL, error REAL, abs_error REAL,
 kronos_median REAL, factor_sign INTEGER, kronos_sign INTEGER, realized_sign INTEGER, bucket TEXT,
 created_at TEXT NOT NULL, PRIMARY KEY(verification_id, symbol));
CREATE TABLE IF NOT EXISTS metrics (
 verification_id TEXT PRIMARY KEY, day TEXT NOT NULL, scope TEXT NOT NULL, horizon INTEGER NOT NULL,
 source_read_ref TEXT NOT NULL, revision INTEGER NOT NULL, n INTEGER NOT NULL, requested INTEGER NOT NULL,
 coverage REAL, rank_ic REAL, kronos_rank_ic REAL, rank_agree REAL, sign_agree REAL, disagreement_rate REAL,
 hit_rate REAL, topk_precision REAL, topk_overlap REAL, mean_ret REAL, median_ret REAL, mean_ret_net_2x REAL,
 mae REAL, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations (
 day TEXT NOT NULL, gate TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(day, gate));
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, detail_json TEXT NOT NULL);
''')
        conn.commit()
        if 'origin' not in {row[1] for row in conn.execute('PRAGMA table_info(days)')}:
            conn.execute("ALTER TABLE days ADD COLUMN origin TEXT NOT NULL DEFAULT 'live'")
            conn.commit()
        return closing(conn)

    @staticmethod
    def _ro(path):
        return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)

    def list_days(self, limit=100):
        if not self.index.is_file():
            return {'schema': RULE_VERSION, 'days': []}
        with closing(self._ro(self.index)) as conn:
            rows = conn.execute('SELECT day,status,identity,read_ref,release_id,model_id,horizon,scope,symbols_json,'
                                'bars_sha256,artifact_path,artifact_sha256,created_at,updated_at,origin '
                                'FROM days ORDER BY day DESC LIMIT ?', (int(limit),)).fetchall()
        keys = ('day', 'status', 'identity', 'read_ref', 'release_id', 'model_id', 'horizon', 'scope',
                'symbols', 'bars_sha256', 'artifact_path', 'artifact_sha256', 'created_at', 'updated_at', 'origin')
        result = []
        for row in rows:
            record = dict(zip(keys, row))
            record['symbols'] = json.loads(record['symbols'])
            result.append(record)
        return {'schema': RULE_VERSION, 'days': result}

    def _latest_attempt(self, conn, day):
        row = conn.execute('SELECT identity,payload_json,created_at FROM attempts WHERE day=? ORDER BY rowid DESC LIMIT 1',
                           (day,)).fetchone()
        return row

    def read(self, day):
        from .forward_evidence import timestamp
        day = _day(day, 'day')
        if not self.index.is_file():
            raise KeyError(day)
        with closing(self._ro(self.index)) as conn:
            head = conn.execute('SELECT status,updated_at,origin FROM days WHERE day=?', (day,)).fetchone()
            if head is None:
                raise KeyError(day)
            attempt = self._latest_attempt(conn, day)
            payload = json.loads(attempt[1]) if attempt else {}
            candidates = conn.execute('SELECT verification_id,source_read_ref,revision,payload_json,created_at,rowid '
                                      'FROM verifications WHERE day=? AND horizon=? ORDER BY created_at DESC',
                                      (day, payload.get('horizon'))).fetchall()
            candidates.sort(key=lambda row: (timestamp(row[4]) or -1, row[5]), reverse=True)
            unique_attempt = conn.execute('SELECT count(*) FROM attempts WHERE day=?', (day,)).fetchone()[0] == 1
            verification_row = next((row for row in candidates
                if json.loads(row[3]).get('record_identity') == payload.get('identity')
                or (unique_attempt and not json.loads(row[3]).get('record_identity'))), None)
            metrics_row = conn.execute('SELECT * FROM metrics WHERE verification_id=?',
                                      (verification_row[0],)).fetchone() if verification_row else None
            realized_rows = []
            if verification_row:
                columns = [item[1] for item in conn.execute('PRAGMA table_info(realized)')]
                realized_rows = [dict(zip(columns, row)) for row in conn.execute(
                    'SELECT * FROM realized WHERE verification_id=? ORDER BY symbol', (verification_row[0],))]
                verification = dict(payload=json.loads(verification_row[3]), source_read_ref=verification_row[1],
                                    revision=verification_row[2], created_at=verification_row[4])
            else:
                verification = None
            metrics = None
            if metrics_row:
                columns = [item[1] for item in conn.execute('PRAGMA table_info(metrics)')]
                metrics = dict(zip(columns, metrics_row))
                metrics['details'] = json.loads(metrics.pop('details_json'))
                for key, value in list(metrics.items()):
                    if isinstance(value, np.generic):
                        metrics[key] = value.item()
        payload = json.loads(attempt[1]) if attempt else None
        return {'schema': RULE_VERSION, 'day': day, 'status': head[0], 'updated_at': head[1], 'origin': head[2],
                'record': payload, 'verification': verification,
                'realized': realized_rows, 'metrics': metrics}

    # -- record ------------------------------------------------------------
    def record(self, params, progress=None, check_cancel=None):
        body = dict(params or {})
        if set(body) - RECORD_ALLOWED:
            raise ValueError('Unknown forward record parameters: ' + ', '.join(sorted(set(body) - RECORD_ALLOWED)))
        source = body.get('source', 'local')
        if source not in ('local', 'fusion'):
            raise ValueError('source must be local or fusion')
        if source == 'fusion':
            run_id = body.get('fusion_run_id')
            if not isinstance(run_id, str) or not re.fullmatch(r'fusion-[0-9a-f]{32}', run_id):
                raise ValueError('A valid fusion_run_id is required when source=fusion')
            start = time.monotonic()
            payload = self._collect_fusion(run_id, origin=body.get('origin'), progress=progress,
                                           check_cancel=check_cancel)
            payload['elapsed_seconds'] = round(time.monotonic() - start, 3)
            return self._persist(payload)
        day = _day(body.get('day'), 'day')
        symbols = _symbols(body.get('symbols'))
        horizon = int(body.get('horizon', 5))
        if not 1 <= horizon <= 20:
            raise ValueError('horizon must be an integer in [1,20]')
        max_calls = int(body.get('max_calls', 20))
        budget = float(body.get('budget_seconds', 300.))
        if not 1 <= max_calls <= WATCHLIST_LIMIT or not 0 < budget <= 1800:
            raise ValueError('max_calls must be in [1,200] and budget_seconds in (0,1800]')
        top_k = int(body.get('top_k', 10))
        if not 1 <= top_k <= 100:
            raise ValueError('top_k must be an integer in [1,100]')
        device = resolve_device(body.get('device'))
        origin = body.get('origin', 'live')
        start = time.monotonic()
        release = ReleaseRegistry(self.root).resolve({'release_id': body['release_id']}) if body.get('release_id') else ReleaseRegistry(self.root).active()
        if release is None:
            raise ValueError('No frozen strategy release is available for forward recording')
        model = self.registry.read_model(release['model_id'])
        if day <= str(model['information_cutoff'])[:10]:
            raise ValueError('Forward recording must follow all training and validation information')
        session = DatasetSession(self.root, body.get('read_ref'), check_cancel)
        calendar = self.workflow._sessions(session, day, {})
        if day not in calendar:
            raise ValueError('The requested day is not a verified trading session')
        _validate_origin(day, self._latest_published(session), origin)
        payload = self._collect(day, symbols, horizon, model, release, session, max_calls, budget, top_k, device,
                                calendar=calendar, origin=origin, progress=progress, check_cancel=check_cancel)
        payload['elapsed_seconds'] = round(time.monotonic() - start, 3)
        session.verify()
        return self._persist(payload)

    def _latest_published(self, session, end=None):
        end = end or date.today().isoformat()
        window = (date.fromisoformat(end) - timedelta(days=30)).isoformat()
        evidence = session.source_files('daily_bars', window, end)
        files = [row['path'] for row in evidence.get('files', [])]
        if not files:
            return None
        value = (pl.scan_parquet(files, hive_partitioning=False)
                 .filter(pl.col('trade_date') <= date.fromisoformat(end))
                 .select(pl.col('trade_date').max()).collect().item())
        return str(value)[:10] if value is not None else None

    def _history_start(self, model):
        return self.workflow._history_start(model)

    def _bars_identity(self, frame, day, symbols):
        if frame is None or frame.empty:
            return None
        subset = frame.loc[frame.date.eq(day) & frame.symbol.isin(symbols), ['symbol', *BAR_COLUMNS]].copy()
        subset = subset.sort_values('symbol')
        records = [[str(row.symbol)] + [_round(value) for value in row[1:]] for row in subset.itertuples(index=False)]
        return hashlib.sha256(canonical(records).encode()).hexdigest()

    def _collect(self, day, symbols, horizon, model, release, session, max_calls, budget, top_k, device,
                 calendar=None, origin='live', progress=None, check_cancel=None):
        from .fusion import FusionService
        history_start = self._history_start(model)
        data = build_dataset(self.root, dict(symbols=symbols, start=history_start, end=day, horizon=model['horizon'],
                                             factor_groups=model['factor_groups']),
                             session=session, check_cancel=check_cancel)
        frame = data['frame']
        day_rows = frame.loc[frame.date.eq(day)].copy()
        if day_rows.empty:
            raise ValueError('No built factor rows for the requested session')
        eligible = day_rows.loc[day_rows.feature_eligible.eq(True) & day_rows.legacy_ma_eligible.eq(True)].copy()
        scores = {}
        if len(eligible):
            eligible['score'] = load_saved(self.registry.directory_for(model['model_id']))(eligible)
            eligible = eligible.sort_values(['score', 'symbol'], ascending=[False, True])
            scores = {str(row.symbol): (float(row.score), int(index + 1))
                      for index, row in enumerate(eligible.itertuples())}
        reasons = {}
        for symbol in symbols:
            if symbol in scores:
                continue
            rows = day_rows.loc[day_rows.symbol.eq(symbol)]
            if rows.empty:
                reasons[symbol] = 'no_built_row'
            elif not bool(rows.iloc[0].bar_valid):
                reasons[symbol] = 'invalid_bar'
            elif not bool(rows.iloc[0].feature_eligible):
                reasons[symbol] = 'feature_history_incomplete'
            elif not bool(rows.iloc[0].legacy_ma_eligible):
                reasons[symbol] = 'legacy_ma_history_incomplete'
            else:
                reasons[symbol] = 'not_scored'
        if not scores:
            raise ValueError('No eligible frozen-model rows for the requested session')
        fusion = FusionService(self.root)
        frames, quotes, following, market = fusion._market_inputs(
            dict(read_ref=session.snapshot['read_ref'], as_of=day), symbols, int(128))
        if len(following) < horizon:
            raise ValueError('Not enough verified future trading sessions for the requested horizon')
        kronos = dict(lookback=128, horizon=horizon, samples=8, seed=42, device=device, temperature=.8, top_p=.9)
        kronos_rows, calls, started = {}, 0, time.monotonic()
        for symbol in symbols:
            check_stop(check_cancel)
            row = dict(symbol=symbol, status='deferred', prediction_id=None, summary=None, quality=None,
                       resource=None, reused=None, reason=None)
            if calls >= max_calls or time.monotonic() - started >= budget:
                row['reason'] = 'budget_not_consumed'
                kronos_rows[symbol] = row
                continue
            calls += 1
            try:
                forecast = fusion._forecast(frames[symbol], following[:horizon], symbol, day, kronos, check_cancel)
            except (InterruptedError,):
                raise
            except Exception as error:  # keep per-symbol failures visible instead of aborting the day
                from lake_equity.jobs import JobCancelled
                if isinstance(error, JobCancelled):
                    raise
                row.update(status='failed', reason=f'{type(error).__name__}: {error}')
                kronos_rows[symbol] = row
                continue
            usable = (forecast.get('quality') or {}).get('prediction_usable') is True
            summary = forecast.get('summary') or {}
            row.update(status='succeeded' if usable else 'unusable', prediction_id=forecast.get('cache_key'),
                       summary=summary, quality=forecast.get('quality'), resource=forecast.get('resource'),
                       reused=bool((forecast.get('resource') or {}).get('reused_observation')))
            if not usable:
                row['reason'] = 'prediction_quality_unusable'
            kronos_rows[symbol] = row
            if progress:
                progress(dict(phase='kronos_forward', current=calls, total=len(symbols), symbol=symbol))
        predictions = []
        signals = []
        created = _now()
        for symbol in symbols:
            score, rank = scores.get(symbol, (None, None))
            kronos_row = kronos_rows[symbol]
            median = _number((kronos_row.get('summary') or {}).get('terminal_return_median'))
            direction = None
            if score is not None:
                direction = 'BUY' if kronos_row['status'] == 'succeeded' and median is not None and median > 0 else 'WAIT'
            signals.append(dict(symbol=symbol, source='factor', model_id=model['model_id'], score=score, rank=rank,
                                direction=direction, expected_return=None,
                                quality={'reason': reasons.get(symbol)},
                                payload_sha256=digest_value(dict(symbol=symbol, score=score, rank=rank,
                                                                 read_ref=session.snapshot['read_ref'])),
                                created_at=created))
            predictions.append(dict(symbol=symbol, factor_score=score, factor_rank=rank,
                                    kronos_median=median if kronos_row['status'] == 'succeeded' else None,
                                    expected_return=None))
        agreement = verification.prediction_agreement(predictions, top_k=top_k)
        agreement_gate = _agreement_gate(agreement)
        kronos_counts = {status: sum(1 for row in kronos_rows.values() if row['status'] == status)
                         for status in ('succeeded', 'unusable', 'failed', 'deferred')}
        payload = dict(
            schema='forward_record_v1', rule_version=RULE_VERSION, code_sha=digest(Path(__file__)), day=day,
            origin=origin, data_cutoff=day, model_horizon=int(model.get('horizon') or 0),
            read_ref=session.snapshot['read_ref'], release_id=release['release_id'], model_id=model['model_id'],
            model_version=model['version'], series_id=release.get('series_id'),
            information_cutoff=str(model['information_cutoff'])[:10], horizon=horizon, scope='symbols',
            symbols=symbols, top_k=top_k, bars_sha256=self._bars_identity(frame, day, symbols),
            device=device_report(), calendar_recent=[item for item in (calendar or []) if item <= day][-32:],
            following_sessions=following[:horizon],
            factor=dict(model_name=model.get('model_name'), model_type=model.get('model_type'),
                        factor_groups=model.get('factor_groups'), scores={symbol: dict(score=value[0], rank=value[1])
                                                                          for symbol, value in scores.items()},
                        unavailable=reasons),
            kronos=dict(params=kronos, coverage=kronos_counts, rows=kronos_rows),
            quotes={symbol: quotes.get(symbol) for symbol in symbols},
            signals=signals, agreement=agreement, gates=dict(data=dict(status='complete', reason=None),
                                                             agreement=agreement_gate),
            limitations=['A forward record is research evidence, not an executable order.',
                         'Kronos sample frequency is not a calibrated probability and no numeric expected return is produced.',
                         'Factor scores are cross-sectional ranks; expected_return stays null until a calibrated mapping is registered.',
                         'Backfill records are reconstructed from data available up to the signal day; the decision to backfill a day was made later.']
            if origin == 'backfill' else
            ['A forward record is research evidence, not an executable order.',
             'Kronos sample frequency is not a calibrated probability and no numeric expected return is produced.',
             'Factor scores are cross-sectional ranks; expected_return stays null until a calibrated mapping is registered.'],
            created_at=created)
        from . import fusion as fusion_module
        payload['implementation_sha256'] = {path.name: digest(path) for path in
                                         (Path(__file__), Path(verification.__file__), Path(fusion_module.__file__))}
        payload['identity'] = hashlib.sha256(canonical({
            'day': day, 'origin': origin, 'read_ref': payload['read_ref'], 'release_id': payload['release_id'],
            'model_id': payload['model_id'], 'symbols': symbols, 'horizon': horizon,
            'kronos': dict(kronos), 'top_k': top_k, 'implementation': payload['implementation_sha256'],
            'rule_version': RULE_VERSION}).encode()).hexdigest()
        return payload

    def _collect_fusion(self, fusion_run_id, origin=None, progress=None, check_cancel=None):
        """Ingest a saved fusion scan as one candidate cross-section record."""
        from .fusion import FusionService
        run = FusionService(self.root).read_run(fusion_run_id)
        contract = _fusion_contract(run)
        day = contract['day']
        overall = DatasetSession(self.root, None, check_cancel)
        latest = self._latest_published(overall)
        if origin is None:
            origin = 'live' if latest == day else 'backfill'
        _validate_origin(day, latest, origin)
        session = DatasetSession(self.root, contract['read_ref'], check_cancel)
        rows = contract['rows']
        symbols = [row['symbol'] for row in rows]
        sources = {}
        bars = _read(session.store, session.snapshot, 'daily_bars', day, day, symbols, sources, check_cancel, session)
        if bars.empty:
            raise ValueError('The fusion session has no published bars in its pinned data generation')
        bars = bars.copy()
        bars['date'] = pd.to_datetime(bars.trade_date).dt.strftime('%Y-%m-%d')
        calendar = self.workflow._sessions(session, (date.fromisoformat(day) + timedelta(days=120)).isoformat(), {})
        following = [item for item in calendar if item > day][:contract['horizon']]
        if len(following) < contract['horizon']:
            raise ValueError('Not enough verified future trading sessions for the fusion horizon')
        created = _now()
        signals, kronos_rows, predictions, quotes = [], {}, [], {}
        for row in rows:
            symbol = row['symbol']
            score = _number(row.get('factor_score'))
            rank = int(row['factor_rank'])
            status = row.get('kronos_status') if row.get('kronos_status') in ('succeeded', 'unusable', 'failed', 'deferred') else 'failed'
            payload = row.get('kronos') or {}
            summary = payload.get('summary') or {}
            median = _number(summary.get('terminal_return_median')) if status == 'succeeded' else None
            direction = 'BUY' if status == 'succeeded' and median is not None and median > 0 else 'WAIT'
            signals.append(dict(symbol=symbol, source='factor', model_id=contract['model_id'], score=score, rank=rank,
                                direction=direction, expected_return=None,
                                quality={'reason': row.get('reason'), 'fusion_run_id': fusion_run_id},
                                payload_sha256=digest_value(dict(symbol=symbol, score=score, rank=rank,
                                                                 fusion_run_id=fusion_run_id,
                                                                 read_ref=contract['read_ref'])), created_at=created))
            kronos_rows[symbol] = dict(status=status, prediction_id=row.get('prediction_id'), summary=summary,
                                       quality=payload.get('quality'), resource=payload.get('resource'),
                                       reused=bool((payload.get('resource') or {}).get('reused_observation')),
                                       reason=row.get('reason'))
            predictions.append(dict(symbol=symbol, factor_score=score, factor_rank=rank,
                                    kronos_median=median, expected_return=None))
            quotes[symbol] = dict(reference_close=_number(row.get('reference_close')),
                                  prior_volume=_number(row.get('prior_volume')),
                                  atr_pct=_number(row.get('atr_pct')),
                                  reference_date=row.get('reference_date') or day,
                                  quote_usable=bool(row.get('quote_usable')),
                                  quote_reason=row.get('quote_reason'), source=row.get('source'))
        agreement = verification.prediction_agreement(predictions, top_k=10)
        counts = {status: sum(1 for row in kronos_rows.values() if row['status'] == status)
                  for status in ('succeeded', 'unusable', 'failed', 'deferred')}
        factor_scores = {row['symbol']: dict(score=_number(row.get('factor_score')), rank=int(row['factor_rank']))
                         for row in rows}
        payload = dict(
            schema='forward_record_v1', rule_version=RULE_VERSION, code_sha=digest(Path(__file__)),
            day=day, origin=origin, data_cutoff=day,
            model_horizon=int(((contract['release'].get('protocol') or {}).get('horizon')) or 0),
            read_ref=contract['read_ref'], release_id=contract['release_id'], model_id=contract['model_id'],
            model_version=contract['release'].get('model_version'), series_id=contract['release'].get('series_id'),
            information_cutoff=contract['information_cutoff'], horizon=contract['horizon'], scope='symbols',
            symbols=symbols, top_k=10, bars_sha256=self._bars_identity(bars, day, symbols),
            device=device_report(), calendar_recent=[item for item in calendar if item <= day][-32:],
            following_sessions=following,
            factor=dict(model_name=((contract['release'].get('protocol') or {}).get('model_name')),
                        model_type=((contract['release'].get('protocol') or {}).get('model_type')),
                        factor_groups=((contract['release'].get('protocol') or {}).get('factor_groups')),
                        scores=factor_scores, unavailable={}),
            kronos=dict(params=contract['kronos'], coverage=counts, rows=kronos_rows,
                        source='fusion', fusion_run_id=fusion_run_id),
            quotes=quotes, signals=signals, agreement=agreement,
            gates=dict(data=dict(status='complete' if run.get('status') == 'completed' else 'partial',
                                 reason='fusion_status:' + str(run.get('status'))),
                       agreement=_agreement_gate(agreement)),
            limitations=['A forward record is research evidence, not an executable order.',
                         'This candidate cross-section was ingested from a saved fusion scan; its factor ranks and '
                         'Kronos observations keep that run\'s contract and pinned data generation.',
                         'Kronos sample frequency is not a calibrated probability and no numeric expected return is produced.',
                         'Factor scores are cross-sectional ranks; expected_return stays null until a calibrated mapping is registered.'],
            created_at=created)
        from . import fusion as fusion_module
        payload['implementation_sha256'] = {path.name: digest(path) for path in
                                         (Path(__file__), Path(verification.__file__), Path(fusion_module.__file__))}
        payload['identity'] = hashlib.sha256(canonical({
            'day': day, 'origin': origin, 'read_ref': payload['read_ref'], 'release_id': payload['release_id'],
            'model_id': payload['model_id'], 'symbols': symbols, 'horizon': contract['horizon'],
            'kronos': dict(contract['kronos']), 'top_k': 10, 'fusion_run_id': fusion_run_id,
            'rows_sha256': digest_value([{key: row.get(key) for key in
                                          ('symbol', 'factor_score', 'factor_rank', 'kronos_status', 'prediction_id')}
                                         for row in rows]),
            'implementation': payload['implementation_sha256'], 'rule_version': RULE_VERSION}).encode()).hexdigest()
        session.verify()
        return payload

    def _persist(self, payload):
        day, identity = payload['day'], payload['identity']
        with self._connect() as conn:
            existing = conn.execute('SELECT status,identity,artifact_path,artifact_sha256 FROM days WHERE day=?',
                                    (day,)).fetchone()
            if existing and existing[1] == identity and existing[0] in ('recorded', 'partial', 'verified'):
                return dict(schema='forward_record_v1', status=existing[0], day=day, identity=identity,
                            reused=True, artifact_path=existing[2], artifact_sha256=existing[3],
                            agreement=payload['agreement'], gates=payload['gates'],
                            kronos=payload['kronos']['coverage'])
            folder = self.directory / 'days' / day
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / ('attempt-' + uuid.uuid4().hex + '.json')
            path.write_text(canonical(payload), encoding='utf-8')
            sha = digest(path)
            created = payload['created_at']
            conn.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)', (day, identity, str(path.relative_to(self.root)), sha, created, canonical(payload)))
            conn.execute('INSERT OR REPLACE INTO days(day,status,identity,read_ref,release_id,model_id,series_id,'
                         'information_cutoff,horizon,scope,symbols_json,bars_sha256,artifact_path,artifact_sha256,'
                         'created_at,updated_at,origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (day, 'recorded', identity, payload['read_ref'], payload['release_id'], payload['model_id'],
                          payload.get('series_id'), payload['information_cutoff'], payload['horizon'], payload['scope'],
                          canonical(payload['symbols']), payload.get('bars_sha256'), str(path.relative_to(self.root)), sha,
                          created, created, payload.get('origin', 'live')))
            conn.execute('DELETE FROM signals WHERE day=?', (day,))
            for row in payload['signals']:
                conn.execute('INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                             (day, row['symbol'], row['source'], row['model_id'], row['score'], row['rank'],
                              row['direction'], row['expected_return'], canonical(row['quality']),
                              row['payload_sha256'], row['created_at']))
            conn.execute('DELETE FROM kronos WHERE day=?', (day,))
            for symbol, row in payload['kronos']['rows'].items():
                conn.execute('INSERT INTO kronos VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (day, symbol, row.get('prediction_id'), row['status'], canonical(row.get('quality')),
                              canonical(row.get('summary')), hashlib.sha256(canonical(payload['kronos']['params']).encode()).hexdigest(),
                              canonical(row.get('resource')), 1 if row.get('reused') else 0, created))
            for gate, detail in payload['gates'].items():
                conn.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?)',
                             (day, gate, detail['status'], canonical(detail), created))
            conn.execute('INSERT INTO events(at,kind,detail_json) VALUES(?,?,?)',
                         (created, 'forward_recorded', canonical(dict(day=day, identity=identity, read_ref=payload['read_ref'],
                                                                      agreement=payload['agreement'], kronos=payload['kronos']['coverage']))))
            conn.commit()
        return dict(schema='forward_record_v1', status='recorded', day=day, identity=identity, reused=False,
                    artifact_path=str(path.relative_to(self.root)), artifact_sha256=sha,
                    read_ref=payload['read_ref'], release_id=payload['release_id'], model_id=payload['model_id'],
                    horizon=payload['horizon'], origin=payload.get('origin', 'live'),
                    elapsed_seconds=payload.get('elapsed_seconds'),
                    device=payload.get('device'), agreement=payload['agreement'], gates=payload['gates'],
                    kronos=payload['kronos']['coverage'], factor=payload['factor']['scores'])

    # -- verify ------------------------------------------------------------
    def verify(self, params, progress=None, check_cancel=None):
        body = dict(params or {})
        if set(body) - VERIFY_ALLOWED:
            raise ValueError('Unknown forward verify parameters: ' + ', '.join(sorted(set(body) - VERIFY_ALLOWED)))
        if not self.index.is_file():
            return {'schema': 'forward_verify_v1', 'status': 'empty', 'verified': []}
        requested = body.get('day')
        if requested is not None:
            requested = [requested] if isinstance(requested, str) else list(requested)
            requested = [_day(item, 'day') for item in requested]
        with closing(self._ro(self.index)) as conn:
            if requested is None:
                targets = [row[0] for row in conn.execute(
                    "SELECT day FROM days WHERE status IN ('recorded','partial') ORDER BY day")]
            else:
                targets = requested
        session = DatasetSession(self.root, body.get('read_ref'), check_cancel)
        results = []
        for day in targets:
            check_stop(check_cancel)
            with closing(self._ro(self.index)) as conn:
                attempt = self._latest_attempt(conn, day)
                if attempt is None:
                    raise KeyError(day)
                payload = json.loads(attempt[1])
            futures = self._futures(session, day, payload['horizon'])
            if len(futures) < payload['horizon']:
                results.append(dict(day=day, status='pending', reason='future_sessions_not_on_calendar'))
                continue
            result = self._verify_day(day, payload, futures, session, check_cancel)
            results.append(result)
            if progress:
                progress(dict(phase='forward_verify', day=day, status=result['status']))
        session.verify()
        return {'schema': 'forward_verify_v1', 'status': 'completed' if results else 'empty',
                'read_ref': session.snapshot['read_ref'], 'verified': results}

    def _futures(self, session, day, horizon):
        end = (date.fromisoformat(day) + timedelta(days=120)).isoformat()
        calendar = self.workflow._sessions(session, end, {})
        return [item for item in calendar if item > day][:int(horizon)]

    def _verify_day(self, day, payload, futures, session, check_cancel):
        symbols = payload['symbols']
        horizon = payload['horizon']
        sources = {}
        bars = _read(session.store, session.snapshot, 'daily_bars', day, futures[-1], symbols, sources, check_cancel, session)
        if bars.empty:
            return dict(day=day, status='pending', reason='no_published_future_bars')
        bars = bars.copy()
        bars['date'] = pd.to_datetime(bars.trade_date).dt.strftime('%Y-%m-%d')
        published = set(bars.date)
        if not set(futures) <= published:
            return dict(day=day, status='pending', reason='future_session_not_published',
                        missing=[item for item in futures if item not in published])
        source_read_ref = session.snapshot['read_ref']
        revision = 0 if self._bars_identity(bars, day, symbols) == payload.get('bars_sha256') else 1
        with closing(self._ro(self.index)) as conn:
            candidates = conn.execute('SELECT m.verification_id,v.payload_json FROM metrics m '
                                     'JOIN verifications v ON v.verification_id=m.verification_id '
                                     'WHERE m.day=? AND m.horizon=? AND m.source_read_ref=? AND m.revision=? '
                                     'ORDER BY m.created_at DESC', (day, horizon, source_read_ref, revision)).fetchall()
            existing = next((row for row in candidates
                             if json.loads(row[1]).get('record_identity') == payload['identity']), None)
        if existing:
            return dict(day=day, status='reused', verification_id=existing[0])
        realized = {symbol: verification.realized_return(bars, symbol, day, futures, horizon) for symbol in symbols}
        predictions = []
        for row in payload['signals']:
            kronos = (payload['kronos']['rows'].get(row['symbol']) or {})
            median = _number((kronos.get('summary') or {}).get('terminal_return_median')) if kronos.get('status') == 'succeeded' else None
            predictions.append(dict(symbol=row['symbol'], factor_score=row.get('score'), factor_rank=row.get('rank'),
                                    kronos_median=median, expected_return=None))
        metrics = verification.cross_section(predictions, realized, top_k=payload.get('top_k', 10))
        verification_id = 'verify-' + uuid.uuid4().hex
        created = _now()
        folder = self.directory / 'verifications' / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (verification_id + '.json')
        document = dict(schema='forward_verify_document_v1', day=day, verification_id=verification_id,
                        record_identity=payload['identity'],
                        source_read_ref=source_read_ref, revision=revision, futures=futures,
                        realized=realized, metrics=metrics, created_at=created)
        path.write_text(canonical(document), encoding='utf-8')
        sha = digest(path)
        signal_rows = {row['symbol']: row for row in payload['signals']}
        median_rows = {symbol: _number(((payload['kronos']['rows'].get(symbol) or {}).get('summary') or {}).get('terminal_return_median'))
                       for symbol in symbols}
        scores = sorted([row['factor_score'] for row in predictions if row['factor_score'] is not None])
        median_score = float(np.median(scores)) if scores else None
        with self._connect() as conn:
            conn.execute('INSERT INTO verifications VALUES(?,?,?,?,?,?,?,?,?)',
                         (verification_id, day, horizon, source_read_ref, revision, str(path.relative_to(self.root)), sha, canonical(document), created))
            for symbol in symbols:
                outcome = realized[symbol]
                conn.execute('INSERT INTO realized VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (verification_id, day, symbol, horizon, outcome.get('exec_day'), outcome.get('exit_day'),
                              outcome.get('exec_open'), outcome.get('exit_close'), outcome.get('ret_gross'),
                              outcome.get('ret_net'), outcome.get('ret_net_2x'), 1 if outcome.get('executable') else 0,
                              outcome.get('status'), outcome.get('reason'), created))
                row = signal_rows.get(symbol) or {}
                kronos_median = median_rows.get(symbol)
                factor_sign = verification.factor_direction(row.get('score'), median_score)
                k_sign = verification.kronos_direction(kronos_median)
                realized_value = outcome.get('ret_gross')
                conn.execute('INSERT INTO deviations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (verification_id, day, symbol, horizon, row.get('score'), row.get('rank'), realized_value,
                              None, None, kronos_median, factor_sign, k_sign,
                              None if realized_value is None else (1 if realized_value > 0 else -1 if realized_value < 0 else 0),
                              verification.bucket_name(factor_sign, k_sign) if factor_sign is not None and k_sign is not None else None,
                              created))
            conn.execute('INSERT INTO metrics(verification_id,day,scope,horizon,source_read_ref,revision,n,requested,'
                         'coverage,rank_ic,kronos_rank_ic,rank_agree,sign_agree,disagreement_rate,hit_rate,'
                         'topk_precision,topk_overlap,mean_ret,median_ret,mean_ret_net_2x,mae,details_json,created_at) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (verification_id, day, payload['scope'], horizon, source_read_ref, revision,
                          metrics['n'], metrics['requested'], metrics['coverage'], metrics['rank_ic'],
                          metrics['kronos_rank_ic'], metrics['rank_agree'], metrics['sign_agree'],
                          metrics['disagreement_rate'], metrics['hit_rate'], metrics['topk_precision'],
                          metrics['topk_overlap'], metrics['mean_ret'], metrics['median_ret'],
                          metrics['mean_ret_net_2x'], metrics['mae'], canonical(metrics), created))
            status = 'verified' if metrics['n'] and not metrics['pending'] else 'partial'
            conn.execute('UPDATE days SET status=?, updated_at=? WHERE day=?', (status, created, day))
            gate = dict(status='observed' if metrics['n'] else 'pending',
                        reason=None if metrics['n'] else 'no_executable_realized_rows',
                        n=metrics['n'], coverage=metrics['coverage'])
            conn.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?)',
                         (day, 'realized', gate['status'], canonical(gate), created))
            conn.execute('INSERT INTO events(at,kind,detail_json) VALUES(?,?,?)',
                         (created, 'forward_verified', canonical(dict(day=day, verification_id=verification_id,
                                                                      source_read_ref=source_read_ref, revision=revision,
                                                                      n=metrics['n'], coverage=metrics['coverage'],
                                                                      pending=metrics['pending'],
                                                                      unexecutable=metrics['unexecutable']))))
            conn.commit()
        return dict(day=day, status=status, verification_id=verification_id, revision=revision,
                    source_read_ref=source_read_ref, futures=futures, metrics=metrics, artifact_sha256=sha)

    # -- metrics -----------------------------------------------------------
    def _metric_rows(self):
        from .forward_evidence import annotate, timestamp
        if not self.index.is_file():
            return []
        with closing(self._ro(self.index)) as conn:
            columns = ['_rowid'] + [item[1] for item in conn.execute('PRAGMA table_info(metrics)')]
            raw = [dict(zip(columns, row)) for row in conn.execute(
                'SELECT rowid,* FROM metrics ORDER BY created_at DESC, rowid DESC')]
            raw.sort(key=lambda row: (timestamp(row['created_at']) or -1, row['_rowid']), reverse=True)
            latest = {}
            for row in raw:
                document = conn.execute('SELECT payload_json FROM verifications WHERE verification_id=?',
                                        (row['verification_id'],)).fetchone()
                identity = (json.loads(document[0]) if document else {}).get('record_identity')
                candidates = [json.loads(item[0]) for item in conn.execute(
                    'SELECT payload_json FROM attempts WHERE day=? ORDER BY rowid DESC', (row['day'],))]
                verified_at = timestamp(row['created_at'])
                candidates = [item for item in candidates if item.get('horizon') == row['horizon']
                              and ((identity and item.get('identity') == identity) or
                                   (not identity and verified_at is not None
                                    and timestamp(item.get('created_at')) is not None
                                    and timestamp(item.get('created_at')) <= verified_at))]
                record = candidates[0] if candidates else {}
                row['details'] = json.loads(row.pop('details_json'))
                annotate(row, record)
                if not identity and len(candidates) != 1:
                    row['qualification'] = dict(live_eligible=False, reasons=['legacy_record_binding_ambiguous'])
                latest.setdefault((row['day'], canonical(row['group'])), row)
        result = sorted(latest.values(), key=lambda row: (row['day'], timestamp(row['created_at']) or -1, row['_rowid']))
        for row in result:
            row.pop('_rowid')
        return result

    def metrics(self, params=None):
        body = dict(params or {})
        if set(body) - {'limit'}:
            raise ValueError('Unknown forward metrics parameters')
        limit = max(1, min(500, int(body.get('limit', 60))))
        if not self.index.is_file():
            return {'schema': 'forward_metrics_v1', 'days': [], 'summary': None}
        from .forward_evidence import aggregate
        rows = self._metric_rows()
        selected = [row for row in rows if row['group'] == rows[-1]['group']] if rows else []
        return {'schema': 'forward_metrics_v1', 'days': rows[-limit:],
                'summary': aggregate(selected, _aggregate_buckets)}

    def evaluations(self, day=None):
        if not self.index.is_file():
            return {'schema': 'forward_evaluations_v1', 'gates': []}
        with closing(self._ro(self.index)) as conn:
            if day is None:
                day = conn.execute('SELECT day FROM evaluations ORDER BY day DESC LIMIT 1').fetchone()
                day = day[0] if day else None
            rows = [dict(day=row[0], gate=row[1], status=row[2], detail=json.loads(row[3]), created_at=row[4])
                    for row in conn.execute('SELECT day,gate,status,detail_json,created_at FROM evaluations WHERE day=? '
                                            'ORDER BY gate', (day,))] if day else []
        return {'schema': 'forward_evaluations_v1', 'day': day, 'gates': rows}

    def summary(self, params=None):
        """Read-only dashboard payload: counts, rolling evidence, gates and coverage."""
        body = dict(params or {})
        if set(body) - {'limit'}:
            raise ValueError('Unknown forward summary parameters')
        limit = max(1, min(500, int(body.get('limit', 20))))
        if not self.index.is_file():
            return {'schema': 'forward_summary_v1', 'days': {'total': 0}, 'rolling': None,
                    'gates': [], 'kronos': None, 'sample': {'verified_days': 0, 'required_days': 40,
                                                             'status': 'insufficient'}}
        days = self.list_days(limit=-1)['days']
        metrics = self.metrics({'limit': limit})
        with closing(self._ro(self.index)) as conn:
            payloads = [json.loads(row[0]) for row in conn.execute(
                'SELECT payload_json FROM attempts a WHERE rowid=(SELECT MAX(rowid) FROM attempts b WHERE b.day=a.day) '
                'ORDER BY day DESC')]
        kronos = {status: 0 for status in ('succeeded', 'unusable', 'failed', 'deferred')}
        for payload in payloads:
            for status, count in ((payload.get('kronos') or {}).get('coverage') or {}).items():
                if status in kronos:
                    kronos[status] += int(count or 0)
        requested = sum(kronos.values())
        all_metrics = self._metric_rows()
        verified = [row for row in all_metrics if row.get('n')]
        live = [row for row in verified if row['qualification']['live_eligible']]
        groups = {}
        for row in live:
            groups.setdefault(canonical(row['group']), []).append(row)
        selected = live[-1]['group'] if live else None
        count = len({row['day'] for row in live if row['group'] == selected})
        sample = dict(verified_days=count, required_days=40, selected_group=selected,
                      historical_verified_days=len({row['day'] for row in verified if row['origin'] == 'backfill'}),
                      total_qualified_live_days=len({row['day'] for row in live}),
                      groups=[dict(group=rows[0]['group'], verified_days=len({row['day'] for row in rows}))
                              for rows in groups.values()],
                      status='sufficient' if count >= 40 else 'insufficient',
                      paper_allocation_eligible=False)
        latest = verified[-1] if verified else None
        counts = dict(total=len(days),
                      recorded=sum(1 for row in days if row['status'] == 'recorded'),
                      verified=sum(1 for row in days if row['status'] == 'verified'),
                      partial=sum(1 for row in days if row['status'] == 'partial'),
                      live=sum(1 for row in days if row.get('origin') == 'live'),
                      backfill=sum(1 for row in days if row.get('origin') == 'backfill'))
        return {'schema': 'forward_summary_v1', 'days': counts, 'latest': latest,
                'rolling': (metrics['summary'] or {}).get('rolling'), 'metrics': metrics['days'],
                'gates': self.evaluations()['gates'],
                'kronos': dict(**kronos, requested=requested,
                               coverage=(kronos['succeeded'] / requested) if requested else None),
                'sample': sample,
                'limitations': ['历史补录不计入实时前瞻；独立日按同一发布、模型及目标周期分别累计。40 日仅为研究复核门槛，不代表配置资格。',
                                '滚动窗口是最近已记录信号日，不是连续交易日；缺失日不补零。',
                                '旧账本的因子相对中位数方向、开盘至收盘收益与 Kronos 收盘至收盘目标不同，一致率仅作历史描述。',
                                '1 日开盘至当日收盘不是 A 股新建仓可执行回合；成本扣减不等于真实交易回测。']}


def _aggregate_buckets(rows):
    totals = {name: dict(count=0, wins=0, total=0.) for name in ('A', 'B', 'C', 'D')}
    for row in rows:
        for name, bucket in ((row.get('details') or {}).get('buckets') or {}).items():
            if name not in totals or not bucket.get('count'):
                continue
            totals[name]['count'] += int(bucket['count'])
            totals[name]['wins'] += int(round((bucket.get('hit_rate') or 0.) * bucket['count']))
            totals[name]['total'] += float(bucket.get('mean_ret') or 0.) * bucket['count']
    result = {}
    for name, bucket in totals.items():
        count = bucket['count']
        result[name] = dict(count=count, mean_ret=bucket['total'] / count if count else None,
                            hit_rate=bucket['wins'] / count if count else None)
    return result


def _round(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if np.isfinite(number) else None


def digest_value(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()
