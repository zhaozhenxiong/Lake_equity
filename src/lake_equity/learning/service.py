"""Auditable lake-to-model experiments; no promotion or existing-model mutation."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

import numpy as np
import pandas as pd

from .models import check_stop, fit_predict, rolling_folds, signal_metrics, validate_model_options


FAMILY = 'cross_sectional_return_v1'
DEFAULTS = dict(factor_groups=['price_volume', 'margin'], horizon=5, train_sessions=252,
                valid_sessions=63, test_sessions=63, max_folds=3, top_k=10,
                initial_cash=1_000_000., backend='local', models=['ridge', 'lightgbm', 'mlp'], seed=42,
                scope='symbols', model_options=None)
GROUPS = [dict(id='price_volume', label='量价与均线（38项）', datasets=['daily_bars']),
          dict(id='margin', label='融资融券（5项，研究滞后假设）', datasets=['margin_trading']),
          dict(id='money_flow', label='资金流（按真实观察时间接入）', datasets=['fund_flow', 'fund_flow_ths']),
          dict(id='legacy_ma', label='原20维均线对照', datasets=['daily_bars'])]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _hash(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def validate_spec(request):
    if not isinstance(request, dict):
        raise ValueError('Learning parameters must be an object')
    unknown = set(request) - set(DEFAULTS) - {'symbols', 'start', 'end', 'read_ref'}
    if unknown:
        raise ValueError('Unknown learning parameters: ' + ', '.join(sorted(unknown)))
    spec = {**DEFAULTS, **request}
    for key in ('start', 'end'):
        value = spec.get(key)
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError(key + ' must be an ISO date')
    if spec['start'] > spec['end']:
        raise ValueError('start must not exceed end')
    symbols = spec.get('symbols')
    if spec['scope'] not in {'symbols', 'all'}:
        raise ValueError('scope must be symbols or all')
    if spec['scope'] == 'all' and symbols not in (None, []):
        raise ValueError('Do not combine all-market scope with an explicit symbol list')
    if spec['scope'] == 'all':
        symbols = []
        spec['symbols'] = []
    if not isinstance(symbols, list) or (spec['scope'] != 'all' and not 1 <= len(symbols) <= 6000) or any(
            not isinstance(s, str) or not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', s) for s in symbols):
        raise ValueError('Provide 1..6000 explicit Shanghai/Shenzhen A-share symbols')
    if len(set(symbols)) != len(symbols):
        raise ValueError('Duplicate learning symbols')
    bounds = dict(horizon=(1, 20), train_sessions=(40, 1260), valid_sessions=(10, 252),
                  test_sessions=(10, 252), max_folds=(1, 12), top_k=(1, 100), seed=(0, 2147483647))
    for key, (low, high) in bounds.items():
        value = spec[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{key} must be an integer in [{low},{high}]')
    if isinstance(spec['initial_cash'], bool) or not isinstance(spec['initial_cash'], (float, int)) or not 10_000 <= spec['initial_cash'] <= 100_000_000:
        raise ValueError('initial_cash must be between 10000 and 100000000')
    if spec['backend'] not in ('local', 'qlib'):
        raise ValueError('backend must be local or qlib')
    for field, allowed in [('factor_groups', {group['id'] for group in GROUPS}), ('models', {'ridge', 'lightgbm', 'mlp'})]:
        items = spec[field]
        if not isinstance(items, list) or not items or any(not isinstance(item, str) or item not in allowed for item in items) or len(set(items)) != len(items):
            raise ValueError('Invalid ' + field)
    if spec['model_options'] is not None and 'mlp' not in spec['models']:
        raise ValueError('model_options require the MLP model')
    spec['model_options'] = validate_model_options(spec['model_options']) if 'mlp' in spec['models'] else None
    if spec.get('read_ref') is not None and not re.fullmatch('[0-9a-f]{64}', str(spec['read_ref'])):
        raise ValueError('Invalid read_ref')
    return spec


class LearningService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.runs_root = self.root / 'var/learning/runs'
        self.index = self.root / 'var/learning/runs.db'

    def catalog(self):
        python = self.root / 'var/runtimes/qlib' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        local = all(importlib.util.find_spec(name) is not None for name in ('sklearn', 'lightgbm', 'pyarrow'))
        runtime = python.parent.parent / 'verified.json'
        return dict(family=FAMILY, factor_groups=GROUPS, defaults=DEFAULTS, limits={'max_symbols': 6000, 'feature_batch_size': 200},
                    models=['ridge', 'lightgbm', 'mlp'], model_options=validate_model_options(None),
                    backends=[dict(id='local', available=local, reason=None if local else 'Install project learning extras'),
                              dict(id='qlib', available=python.is_file() and runtime.is_file(),
                                   reason=None if runtime.is_file() else 'Install and verify the isolated Qlib runtime')],
                    qualification=dict(research_only=True, strict_history_eligible=False),
                    note='Read-only capabilities; no capture, feature calculation or training is triggered')

    def list_runs(self, limit=100):
        if not self.index.is_file():
            return {'runs': []}
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as connection:
            rows = connection.execute('SELECT summary FROM runs ORDER BY rowid DESC LIMIT ?', (-1 if limit is None else limit,)).fetchall()
        return {'runs': [json.loads(row[0]) for row in rows]}

    def read_run(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'learn-[0-9a-f]{32}', run_id):
            raise ValueError('Invalid learning run identity')
        if not self.index.is_file():
            raise KeyError(run_id)
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as connection:
            row = connection.execute('SELECT report_sha256 FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        path = self.runs_root / run_id / 'report.json'
        if path.is_symlink() or path.parent.is_symlink() or _hash(path) != row[0]:
            raise ValueError('Saved learning report SHA256 mismatch')
        report = json.loads(path.read_text(encoding='utf-8'))
        if report.get('run_id') != run_id:
            raise ValueError('Saved learning report identity mismatch')
        return report

    def run(self, request, progress=None, check_cancel=None):
        from .dataset import build_dataset
        from .portfolio import backtest_scores
        from my_strategy.core.run_context import RunContext

        spec = validate_spec(request)
        capabilities = self.catalog()
        if not next(row['available'] for row in capabilities['backends'] if row['id'] == spec['backend']):
            raise ValueError('Requested learning backend is not installed and verified; see docs/LEARNING.md')
        check_stop(check_cancel)
        run_id = 'learn-' + uuid.uuid4().hex
        self.runs_root.mkdir(parents=True, exist_ok=True)
        working = self.runs_root / ('.' + run_id + '.building')
        destination = self.runs_root / run_id
        working.mkdir()
        implementation = {path.name: _hash(path) for path in Path(__file__).parent.glob('*.py')}
        (working / 'code').mkdir()
        for path in Path(__file__).parent.glob('*.py'):
            shutil.copyfile(path, working / 'code' / path.name)

        def notify(**payload):
            if progress:
                progress(dict(stage=payload.get('phase', 'learning'), **payload))

        # Failed/cancelled builds remain isolated and unindexed for diagnosis.
        population_manifest = None
        if spec['scope'] == 'all' or len(spec['symbols']) > 500:
            from .dataset import DatasetSession
            from .population import population
            from .partitioned import build_partitioned
            session = DatasetSession(self.root, spec.get('read_ref'), check_cancel)
            population_manifest = population(session, spec.get('symbols'), scope=spec['scope'], end=spec['end'])
            spec = dict(spec, symbols=population_manifest['symbols'], read_ref=session.snapshot['read_ref'])
            data = build_partitioned(self.root, spec, working / 'partitions', session=session,
                                     progress=notify, check_cancel=check_cancel)
        else:
            data = build_dataset(self.root, spec, progress=notify, check_cancel=check_cancel)
        frame = data['frame'].sort_values(['date', 'symbol']).reset_index(drop=True)
        if frame[['date', 'symbol']].duplicated().any():
            raise ValueError('Duplicate dataset keys')
        frame.to_parquet(working / 'dataset.parquet', index=False)
        (working / 'sources.json').write_text(_json(data['sources']), encoding='utf-8')
        (working / 'config.json').write_text(_json(spec), encoding='utf-8')
        columns = list(dict.fromkeys(column for group in spec['factor_groups'] for column in data['feature_groups'][group]))
        for group in spec['factor_groups']:
            group_columns = data['feature_groups'][group]
            if not group_columns or not frame[group_columns].notna().any().any():
                raise ValueError('Required factor group has no available observations: ' + group)
        baseline = data['feature_groups']['legacy_ma']
        experiments = [('legacy_ma_ridge', baseline, 'ridge', 'legacy_ma')]
        if 'mlp' in spec['models']:
            experiments.append(('legacy_ma_mlp', baseline, 'mlp', 'legacy_ma'))
        if 'price_volume' in spec['factor_groups'] and any(group not in ('price_volume', 'legacy_ma') for group in spec['factor_groups']):
            experiments += [('price_volume_' + name, data['feature_groups']['price_volume'], name, 'price_volume')
                            for name in spec['models']]
        experiments += [('factor_' + name, columns, name, 'all_factors') for name in spec['models']]
        folds = rolling_folds(data['calendar'], spec)
        predictions, model_details = [], []
        eligible = frame.feature_eligible.eq(True) & frame.legacy_ma_eligible.eq(True)
        for fold in folds:
            check_stop(check_cancel)
            notify(phase='rolling_train', current=fold['fold'] + 1, total=len(folds), test_start=fold['test_start'])
            train_mask = eligible & frame.date.between(fold['train_start'], fold['train_end']) & frame.label.notna()
            valid_mask = eligible & frame.date.between(fold['valid_start'], fold['valid_end']) & frame.label.notna()
            train = frame.loc[train_mask & frame.label_end.lt(fold['valid_start']).fillna(False)]
            valid = frame.loc[valid_mask & frame.label_end.lt(fold['test_start']).fillna(False)]
            test = frame.loc[eligible & frame.date.between(fold['test_start'], fold['test_end'])]
            if len(train) < 20 or len(valid) < 10 or test.empty:
                raise ValueError(f"Fold {fold['fold']} has insufficient eligible train/valid/inference rows: {len(train)}/{len(valid)}/{len(test)}")
            fit_end = max(train.label_end.max(), valid.label_end.max())
            for group in spec['factor_groups']:
                if not train[data['feature_groups'][group]].notna().any().any():
                    raise ValueError(f"Required factor group is empty in training fold {fold['fold']}: {group}")
            if fit_end >= fold['test_start']:
                raise ValueError('Model selection labels overlap the out-of-sample window')
            fold.update(train_rows=len(train), valid_rows=len(valid), test_rows=len(test), fit_end=fit_end,
                        purged_train_rows=int(train_mask.sum()-len(train)), purged_valid_rows=int(valid_mask.sum()-len(valid)))
            for name, features, model_type, feature_set in experiments:
                scores, details = fit_predict(train, valid, test, features, model_type=model_type,
                    backend=spec['backend'], seed=spec['seed'], directory=working / 'models' / f"fold-{fold['fold']}" / name,
                    root=self.root, check_cancel=check_cancel,
                    model_options=spec['model_options'] if model_type == 'mlp' else None)
                out = test[['date', 'symbol', 'label', 'label_end']].copy()
                out['score'], out['model_name'], out['fold'] = scores, name, fold['fold']
                out['train_end'], out['fit_train_end'] = fit_end, fold['train_end']
                out['available_at'] = out.date + 'T15:00:00+08:00'
                out['input_id'] = data['sources']['read_ref']
                out['feature_schema_sha256'] = hashlib.sha256(_json(details['preprocessing']['columns']).encode()).hexdigest()
                out['model_version'] = _hash(working / 'models' / f"fold-{fold['fold']}" / name / 'model.json')
                predictions.append(out)
                model_details.append(dict(name=name, fold=fold['fold'], feature_set=feature_set,
                                          feature_count=details['feature_count'],
                                          columns=details['preprocessing']['columns'], engine=details['engine'],
                                          versions=details.get('versions'),
                                          group_usage={group: dict(
                                              retained_columns=[column for column in group_columns if column in details['preprocessing']['columns']],
                                              nonempty_training_values=int(train[[column for column in group_columns if column in details['preprocessing']['columns']]].notna().sum().sum()))
                                              for group, group_columns in data['feature_groups'].items()},
                                          dropped_training_empty=details['preprocessing']['dropped_training_empty'],
                                          importance=details.get('importance', {})))
        oos = pd.concat(predictions, ignore_index=True)
        if oos[['model_name', 'date', 'symbol']].duplicated().any():
            raise ValueError('Duplicate out-of-sample predictions across folds')
        oos.to_parquet(working / 'predictions.parquet', index=False)
        models = []
        # Full calendar/price prefix supplies the previous session's executable
        # evidence; the adapter starts its ledger on the first OOS score date.
        execution_calendar = data['calendar']
        first_signal = str(oos.date.min())
        previous_sessions = [day for day in execution_calendar if day < first_signal]
        execution_start = previous_sessions[-1] if previous_sessions else first_signal
        execution_prices = frame.loc[frame.date.ge(execution_start)]
        benchmark_scores = oos.loc[oos.model_name.eq(experiments[0][0])].copy()
        benchmark_scores['score'], benchmark_scores['model_name'] = 0., 'equal_weight_sample'
        benchmark = backtest_scores(execution_prices, benchmark_scores, execution_calendar,
            {'top_k': len(spec['symbols']), 'initial_cash': spec['initial_cash']},
            run_id=run_id + '-equal-weight', check_cancel=check_cancel)
        for name, features, model_type, feature_set in experiments:
            check_stop(check_cancel)
            notify(phase='portfolio_backtest', model=name)
            selected = oos.loc[oos.model_name.eq(name)]
            portfolio = backtest_scores(execution_prices, selected, execution_calendar,
                {'top_k': spec['top_k'], 'initial_cash': spec['initial_cash']}, run_id=run_id + '-' + name,
                check_cancel=check_cancel)
            stress = backtest_scores(execution_prices, selected, execution_calendar,
                {'top_k': spec['top_k'], 'initial_cash': spec['initial_cash'], 'cost_stress': 2.0},
                run_id=run_id + '-' + name + '-cost2', check_cancel=check_cancel)
            fitted = [row for row in model_details if row['name'] == name]
            models.append(dict(name=name, model_type=model_type, feature_set=feature_set,
                               requested_feature_count=len(features), feature_count=fitted[-1]['feature_count'],
                               feature_count_range=[min(row['feature_count'] for row in fitted), max(row['feature_count'] for row in fitted)],
                               metrics=signal_metrics(selected), portfolio=portfolio,
                               excess_return_vs_equal_weight=float(portfolio['metrics']['total_return'] - benchmark['metrics']['total_return']),
                               cost_stress_2x=stress['metrics'],
                               fold_models=fitted))
        if implementation != {path.name: _hash(path) for path in Path(__file__).parent.glob('*.py')}:
            raise RuntimeError('Learning implementation changed during this run; completed publication refused')
        source_id = hashlib.sha256(_json(data['sources']).encode()).hexdigest()
        context = RunContext(run_id=run_id, as_of_date=spec['end'], git_commit='NO_GIT',
                             config_hash=hashlib.sha256(_json(spec).encode()).hexdigest(), data_version=source_id,
                             seed=spec['seed'], artifact_root=str(destination), scope='train', source='api',
                             task=FAMILY, stocks=spec['symbols'], start_date=spec['start'], end_date=spec['end'])
        report = dict(family=FAMILY, status='completed', run_id=run_id, lake_run_id=run_id,
            created_at=datetime.now().astimezone().isoformat(), spec=spec, run_context=asdict(context),
            dataset=data['report'], sources=data['sources'], feature_groups=data['feature_groups'], folds=folds,
            selected_factor_groups=spec['factor_groups'],
            population=population_manifest,
            models=models, implementation_sha256=implementation,
            benchmark=benchmark,
            versions={name: importlib.metadata.version(name) for name in ('numpy', 'pandas', 'scikit-learn', 'lightgbm', 'pyarrow')},
            qualification=dict(research_only=True, strict_history_eligible=False, paper_allocation_eligible=False,
                               independent_forward_status='pending', model_stage='candidate'),
            limitations=data['report'].get('limitations', []) + [
                'Explicit historical sample; this experiment is not a complete historical market or final sealed holdout.',
                'Legacy comparison reproduces its feature formulas with Ridge, not the frozen MLP model or its qualification.',
                'Signal evaluation uses overlapping horizon labels; daily IC observations are not independent.',
                'All models share the same eligible inference rows. Label maturity never filters inference.',
                'Validation determines boosting duration; prediction train_end includes validation label maturity.',
                'No automatic selection, promotion or paper-account allocation occurs.'])
        report['artifacts'] = {str(path.relative_to(working)).replace('\\', '/'):
                              dict(path=str(path.relative_to(working)).replace('\\', '/'), sha256=_hash(path), size_bytes=path.stat().st_size)
                              for path in working.rglob('*') if path.is_file()}
        check_stop(check_cancel)
        (working / 'report.json').write_text(_json(report), encoding='utf-8')
        working.rename(destination)
        from .registry import ModelRegistry
        ModelRegistry(self.root).publish_run(report)
        notify(phase='completed', run_id=run_id)
        return report
