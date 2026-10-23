"""Frozen model inference shared by market selection and broker backtests."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

import numpy as np
import pandas as pd
import polars as pl

from .dataset import DatasetSession, _read, build_dataset
from .models import check_stop, load_saved, rolling_folds
from .population import population
from .registry import ModelRegistry, canonical, digest


def _day(value, name):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError(name + ' must be an ISO date')
    return value


class LearningWorkflow:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.registry = ModelRegistry(root)
        self.directory = self.root / 'var/learning/workflows'
        self.index = self.root / 'var/learning/workflows.db'

    def list_runs(self, kind=None, limit=100):
        if not self.index.is_file():
            return {'reports': []}
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            rows = [json.loads(row[0]) for row in conn.execute('SELECT summary FROM runs ORDER BY rowid DESC LIMIT ?', (limit,))]
        return {'reports': [row for row in rows if kind is None or row['kind'] == kind]}

    def read_run(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'(?:scan|backtest)-[0-9a-f]{32}', run_id):
            raise ValueError('Invalid model workflow identity')
        if not self.index.is_file():
            raise KeyError(run_id)
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT report_sha256 FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        folder = self.directory / run_id
        path = folder / 'report.json'
        if folder.is_symlink() or path.is_symlink() or digest(path) != row[0]:
            raise ValueError('Saved model workflow report SHA256 mismatch')
        result = json.loads(path.read_text(encoding='utf-8'))
        for name, sha in result['artifacts'].items():
            asset = folder / name
            if Path(name).name != name or asset.is_symlink() or digest(asset) != sha:
                raise ValueError('Saved model workflow artifact SHA256 mismatch')
        return result

    def _start(self, kind):
        identity = kind + '-' + uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=True)
        working = self.directory / ('.' + identity + '.building')
        working.mkdir()
        return identity, working

    def _save(self, report, working):
        from my_strategy.core.run_context import RunContext
        context = RunContext(run_id=report['run_id'], as_of_date=report.get('as_of', report['spec'].get('end')),
            git_commit='NO_GIT', config_hash=hashlib.sha256(canonical(report['spec']).encode()).hexdigest(),
            data_version=report['read_ref'], seed=self.registry.read_model(report.get('model_id') or report['model_ids'][0])['seed'],
            artifact_root=str(self.directory / report['run_id']),
            scope=report['kind'], source='api', task=report['family'],
            stocks=report.pop('_symbols'), start_date=report['spec'].get('start'),
            end_date=report.get('as_of', report['spec'].get('end')))
        report['run_context'] = asdict(context)
        report['inference_implementation_sha256'] = {path.name: digest(path) for path in Path(__file__).parent.glob('*.py')}
        report['artifacts'] = {p.name: digest(p) for p in working.iterdir() if p.is_file()}
        path = working / 'report.json'
        path.write_text(canonical(report), encoding='utf-8')
        sha = digest(path)
        working.rename(self.directory / report['run_id'])
        summary = {key: report[key] for key in ['run_id', 'family', 'kind', 'status', 'created_at', 'spec']}
        summary.update(id=report['run_id'], title='模型选股' if report['kind'] == 'scan' else '模型回测',
                       as_of=report.get('as_of', report['spec'].get('end')))
        with closing(sqlite3.connect(self.index, timeout=30)) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, report_sha256 TEXT NOT NULL, summary TEXT NOT NULL)')
            conn.execute('INSERT INTO runs VALUES(?,?,?)', (report['run_id'], sha, canonical(summary)))
            conn.commit()
        return report

    def _sessions(self, session, end, sources):
        frame = _read(session.store, session.snapshot, 'trading_calendar', '1900-01-01', end,
                      None, sources, session.check_cancel, session)
        days = sorted(pd.to_datetime(frame.loc[frame.is_trading.eq(True), 'trade_date']).dt.strftime('%Y-%m-%d').tolist())
        if not days or len(set(days)) != len(days):
            raise ValueError('No unique verified trading calendar')
        return days

    def _history_start(self, model):
        if model.get('feature_history_start'):
            return model['feature_history_start']
        # Early v1 bundles retain this identity in their immutable study.
        from .service import LearningService
        return LearningService(self.root).read_run(model['run_id'])['spec']['start']

    def preflight(self, params):
        from .service import validate_spec
        spec = validate_spec(params)
        session = DatasetSession(self.root, spec.get('read_ref'))
        cohort = population(session, spec.get('symbols'), scope=spec['scope'], end=spec['end'])
        summaries, folds, blockers = [], None, []
        total_rows = total_train = 0
        fold_counts = []
        group_values = {name: 0 for name in spec['factor_groups']}
        for offset in range(0, cohort['total'], 200):
            data = build_dataset(self.root, dict(spec, symbols=cohort['symbols'][offset:offset + 200]), session=session)
            if folds is None:
                try:
                    folds = rolling_folds(data['calendar'], spec)
                except ValueError as error:
                    blockers.append(str(error))
                    folds = []
                fold_counts = [dict(fold=row['fold'], train_rows=0, valid_rows=0, test_rows=0,
                                    factor_nonempty_values={name: 0 for name in spec['factor_groups']}) for row in folds]
            total_rows += len(data['frame'])
            total_train += int(data['frame'].label.notna().sum())
            for name in group_values:
                group_values[name] += data['report']['group_coverage'][name]['nonempty_values']
            frame = data['frame']
            eligible = frame.feature_eligible.eq(True) & frame.legacy_ma_eligible.eq(True)
            for fold, counts in zip(folds, fold_counts):
                train = frame.loc[eligible & frame.date.between(fold['train_start'], fold['train_end']) & frame.label.notna() & frame.label_end.lt(fold['valid_start']).fillna(False)]
                valid = frame.loc[eligible & frame.date.between(fold['valid_start'], fold['valid_end']) & frame.label.notna() & frame.label_end.lt(fold['test_start']).fillna(False)]
                counts['train_rows'] += len(train)
                counts['valid_rows'] += len(valid)
                counts['test_rows'] += int((eligible & frame.date.between(fold['test_start'], fold['test_end'])).sum())
                for name in group_values:
                    counts['factor_nonempty_values'][name] += int(train[data['feature_groups'][name]].notna().sum().sum())
            summaries.append(data['report']['symbols_with_bars'])
        session.verify()
        blockers += ['Required factor group has no available observations: ' + name for name, count in group_values.items() if not count]
        for counts in fold_counts:
            if counts['train_rows'] < 20 or counts['valid_rows'] < 10 or counts['test_rows'] == 0:
                blockers.append(f"Fold {counts['fold']} has insufficient eligible fit/inference rows")
            blockers += [f"Fold {counts['fold']} required training group is empty: {name}" for name, count in counts['factor_nonempty_values'].items() if not count]
        if spec['model_options'] and spec['model_options']['device'] == 'cuda':
            import torch
            if not torch.cuda.is_available():
                blockers.append('Requested CUDA device is unavailable')
        return dict(status='blocked' if blockers else 'ready', family='factor_training_preflight_v1',
            population=cohort, population_inspected=cohort['total'], rows=total_rows,
            mature_label_rows=total_train, symbols_with_bars=sum(summaries), factor_nonempty_values=group_values,
            folds=folds, fold_coverage=fold_counts, blockers=blockers, read_ref=session.snapshot['read_ref'], spec=spec,
            estimated_feature_matrix_bytes=total_rows * 43 * 8,
            memory_policy='200-symbol feature partitions; selected estimator folds remain in memory',
            qualification=dict(research_only=True, strict_history_eligible=False, paper_allocation_eligible=False),
            note='Preflight checks source coverage; every training fold rechecks required groups and label purging.')

    def scan(self, params, progress=None, check_cancel=None):
        allowed = {'model_id', 'scope', 'symbols', 'end', 'read_ref', 'top_k'}
        if set(params) - allowed:
            raise ValueError('Unknown model scan parameters: ' + ', '.join(sorted(set(params) - allowed)))
        if params.get('scope') == 'all' and params.get('symbols'):
            raise ValueError('Do not combine all-market scope with explicit symbols')
        top_k = params.get('top_k', 20)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError('top_k must be an integer in [1,100]')
        end = _day(params.get('end'), 'end')
        model = self.registry.read_model(params.get('model_id'))
        infer = load_saved(self.registry.directory / model['model_id'])
        history_start = self._history_start(model)
        session = DatasetSession(self.root, params.get('read_ref'), check_cancel)
        sources = {}
        calendar = self._sessions(session, end, sources)
        # The requested cutoff may be a holiday or a session whose bars have not
        # been published. Resolve the actual market date, and disclose both.
        evidence = session.source_files('daily_bars', '1900-01-01', end)
        if not evidence['files']:
            raise ValueError('No published daily bars')
        latest = (pl.scan_parquet([row['path'] for row in evidence['files']], hive_partitioning=False)
                  .filter(pl.col('trade_date') <= date.fromisoformat(end)).select(pl.col('trade_date').max()).collect().item())
        if latest is None:
            raise ValueError('No published bars at the requested cutoff')
        as_of = min(calendar[-1], str(latest)[:10])
        if as_of <= model['information_cutoff']:
            raise ValueError('Frozen inference date must follow all training and validation information')
        cohort = population(session, params.get('symbols'), scope=params.get('scope', 'symbols'), end=as_of)
        run_id, working = self._start('scan')
        rows = {symbol: dict(symbol=symbol, score=None, rank=None, status='unprocessed', reason='not_processed',
                             is_training_member=symbol in model['population']) for symbol in cohort['symbols']}
        cancelled = False
        from lake_equity.jobs import JobCancelled
        try:
            for offset in range(0, cohort['total'], 200):
                check_stop(check_cancel)
                symbols = cohort['symbols'][offset:offset + 200]
                data = build_dataset(self.root, dict(symbols=symbols, start=history_start, end=as_of,
                    horizon=model['horizon'], factor_groups=model['factor_groups']), session=session,
                    check_cancel=check_cancel)
                frame = data['frame'].loc[data['frame'].date.eq(as_of)]
                eligible = frame.feature_eligible.eq(True) & frame.legacy_ma_eligible.eq(True)
                candidate = frame.loc[eligible]
                scores = infer(candidate) if len(candidate) else []
                for observation, score in zip(candidate.to_dict('records'), scores):
                    row = rows[observation['symbol']]
                    row.update(score=float(score), status='scored', reason=None,
                               imputed_feature_count=int(sum(not np.isfinite(float(observation[col])) for col in model['columns'])))
                for observation in frame.loc[~eligible].to_dict('records'):
                    rows[observation['symbol']].update(status='missing', reason='no_valid_bar_as_of' if not observation['bar_valid'] else 'insufficient_verified_history')
                if progress:
                    progress(dict(phase='model_scan', current=min(offset + 200, cohort['total']), total=cohort['total'],
                                  model_id=model['model_id'], as_of=as_of))
        except JobCancelled:
            cancelled = True
            session.check_cancel = None
        session.verify()
        counts = {status: sum(row['status'] == status for row in rows.values()) for status in ['scored', 'missing', 'failed', 'unprocessed']}
        complete = counts['unprocessed'] == 0 and counts['failed'] == 0 and not cancelled
        ordered = sorted((row for row in rows.values() if row['status'] == 'scored'), key=lambda row: (-row['score'], row['symbol']))
        if complete:
            for rank, row in enumerate(ordered, 1):
                row['rank'] = rank
        (working / 'population.json').write_text(canonical(cohort), encoding='utf-8')
        pd.DataFrame(rows.values()).to_parquet(working / 'scores.parquet', index=False)
        report = dict(family='model_scan_v1', kind='scan', run_id=run_id, lake_run_id=run_id,
            created_at=datetime.now().astimezone().isoformat(), status='cancelled' if cancelled else 'completed' if complete else 'partial',
            spec=params, model_id=model['model_id'], model_version=model['version'], information_cutoff=model['information_cutoff'],
            frozen_at=model['frozen_at'], as_of=as_of, requested_cutoff=end, end=as_of, scope=cohort['scope'],
            population=dict(total=cohort['total'], **counts, sha256=cohort['sha256']),
            rows=ordered + [row for row in rows.values() if row['status'] != 'scored'],
            top=ordered[:top_k] if complete else [], final_ranking=complete,
            feature_history_start=history_start,
            read_ref=session.snapshot['read_ref'], sources={str(key): value for key, value in session.evidence.items()},
            _symbols=cohort['symbols'],
            qualification=model['qualification'],
            purpose='historical_reconstruction' if as_of < model['frozen_at'][:10] else 'frozen_inference',
            limitations=['Current published population is a frozen research cohort; it is not certified historical membership.',
                          'Missing factors use the original training medians; per-row imputation counts are disclosed.',
                          'Close-derived ranks are research for the following verified session, not executable orders.'])
        return self._save(report, working)

    def backtest(self, params, progress=None, check_cancel=None):
        from .portfolio import backtest_scores
        from .service import LearningService
        allowed = {'mode', 'model_id', 'series_id', 'start', 'end', 'scope', 'symbols', 'read_ref',
                   'top_k', 'initial_cash', 'rebalance_sessions', 'cost_stress', 'require_verified_rules'}
        if set(params) - allowed:
            raise ValueError('Unknown model backtest parameters: ' + ', '.join(sorted(set(params) - allowed)))
        start, end = _day(params.get('start'), 'start'), _day(params.get('end'), 'end')
        if start > end:
            raise ValueError('start must not exceed end')
        mode = params.get('mode', 'frozen')
        if mode not in {'frozen', 'rolling'}:
            raise ValueError('mode must be frozen or rolling')
        if params.get('model_id') and params.get('series_id'):
            raise ValueError('Do not combine a fixed model and a rolling series')
        if params.get('scope') == 'all' and params.get('symbols'):
            raise ValueError('Do not combine all-market scope with explicit symbols')
        run_id, working = self._start('backtest')
        model_ids = []
        if mode == 'rolling':
            series = self.registry.read_series(params.get('series_id'))
            if params.get('scope') == 'all' or params.get('symbols') or params.get('read_ref'):
                raise ValueError('Rolling evaluation uses the original frozen evaluation cohort and data snapshot')
            service = LearningService(self.root)
            report = service.read_run(series['run_id'])
            directory = service.runs_root / series['run_id']
            for name in ['dataset.parquet', 'predictions.parquet']:
                if digest(directory / name) != report['artifacts'][name]['sha256']:
                    raise ValueError('Frozen evaluation dataset SHA256 mismatch')
            if start < min(row['start'] for row in series['members']) or end > max(row['end'] for row in series['members']):
                raise ValueError('Rolling dates must lie within the saved out-of-sample series')
            data = pd.read_parquet(directory / 'dataset.parquet')
            scores = []
            for member in series['members']:
                check_stop(check_cancel)
                model = self.registry.read_model(member['model_id'])
                selected = data.loc[data.date.between(max(start, member['start']), min(end, member['end'])) &
                                    data.feature_eligible.eq(True) & data.legacy_ma_eligible.eq(True)].copy()
                if selected.empty:
                    continue
                selected['score'] = load_saved(self.registry.directory / model['model_id'])(selected)
                selected['fold'], selected['train_end'] = member['fold'], model['information_cutoff']
                selected['model_id'] = model['model_id']
                scores.append(selected)
                model_ids.append(model['model_id'])
            if not scores:
                raise ValueError('No eligible saved out-of-sample observations')
            predictions = pd.concat(scores, ignore_index=True)
            calendar = sorted(set(data.date))
            prices = data.loc[data.date <= end]
            snapshot = report['sources']['read_ref']
            cohort = dict(symbols=report['spec']['symbols'], total=len(report['spec']['symbols']), policy='frozen_original_evaluation_cohort')
            qualification = report['qualification']
            evidence = dict(evaluation_run_id=report['run_id'], sources=report['sources'],
                            evaluation_dataset_sha256=report['artifacts']['dataset.parquet']['sha256'])
        else:
            model = self.registry.read_model(params.get('model_id'))
            if start <= model['information_cutoff']:
                raise ValueError('Fixed-model backtest must begin after all training and validation information')
            infer = load_saved(self.registry.directory / model['model_id'])
            history_start = self._history_start(model)
            model_ids.append(model['model_id'])
            session = DatasetSession(self.root, params.get('read_ref'), check_cancel)
            calendar = self._sessions(session, end, {})
            before = [day for day in calendar if day < start]
            prefix = before[-1] if before else start
            cohort = population(session, None if params.get('scope') == 'all' else params.get('symbols') or model['population'],
                                scope=params.get('scope', 'symbols'), end=end)
            price_parts, score_parts = [], []
            for offset in range(0, cohort['total'], 200):
                check_stop(check_cancel)
                data = build_dataset(self.root, dict(symbols=cohort['symbols'][offset:offset + 200], start=history_start,
                    end=end, horizon=model['horizon'], factor_groups=model['factor_groups']), session=session, check_cancel=check_cancel)
                frame = data['frame']
                from .execution import PRICE_COLUMNS, EXECUTION_EVIDENCE_COLUMNS
                evidence_columns = PRICE_COLUMNS + EXECUTION_EVIDENCE_COLUMNS
                price_parts.append(frame.loc[frame.date.ge(prefix), [column for column in evidence_columns if column in frame]])
                selected = frame.loc[frame.date.ge(start) & frame.feature_eligible.eq(True) & frame.legacy_ma_eligible.eq(True)].copy()
                if len(selected):
                    selected['score'] = infer(selected)
                    selected['fold'], selected['train_end'] = model['fold'], model['information_cutoff']
                    selected['model_id'] = model['model_id']
                    score_parts.append(selected)
                if progress:
                    progress(dict(phase='model_backtest_inference', current=min(offset + 200, cohort['total']), total=cohort['total']))
            session.verify()
            if not score_parts:
                raise ValueError('No eligible frozen-model inference rows for this period')
            prices, predictions = pd.concat(price_parts, ignore_index=True), pd.concat(score_parts, ignore_index=True)
            snapshot, qualification = session.snapshot['read_ref'], model['qualification']
            evidence = {str(key): value for key, value in session.evidence.items()}
        predictions['model_name'] = params.get('series_id') if mode == 'rolling' else model_ids[0]
        predictions['available_at'] = predictions.date + 'T15:00:00+08:00'
        calendar = [day for day in calendar if day <= end]
        predictions[['date', 'symbol', 'score', 'model_name', 'fold', 'train_end', 'model_id', 'available_at']].to_parquet(working / 'scores.parquet', index=False)
        from .execution import PRICE_COLUMNS, EXECUTION_EVIDENCE_COLUMNS
        prices.loc[prices.date.le(end), [column for column in PRICE_COLUMNS + EXECUTION_EVIDENCE_COLUMNS if column in prices]].to_parquet(working / 'prices.parquet', index=False)
        config = {key: params[key] for key in ['top_k', 'initial_cash', 'rebalance_sessions', 'cost_stress', 'require_verified_rules'] if key in params}
        portfolio = backtest_scores(prices, predictions, calendar, config, run_id=run_id, check_cancel=check_cancel)
        (working / 'population.json').write_text(canonical(cohort), encoding='utf-8')
        report = dict(family='model_backtest_v1', kind='backtest', run_id=run_id, lake_run_id=run_id,
            created_at=datetime.now().astimezone().isoformat(), status='completed', spec=params, mode=mode,
            model_id=params.get('model_id'), series_id=params.get('series_id'), model_ids=model_ids,
            portfolio=portfolio, population=cohort, read_ref=snapshot, qualification=qualification,
            model_versions={identity: self.registry.read_model(identity)['version'] for identity in model_ids},
            sources=evidence, _symbols=cohort['symbols'],
            purpose='historical_reconstruction',
            limitations=['The experiment was frozen at the recorded wall-clock time; reconstruction does not prove historical live availability.',
                          'Current or explicit cohorts are not certified point-in-time historical membership. No automatic paper allocation.'])
        return self._save(report, working)
