"""Resumable chronological studies with a separately frozen research winner.

Development selection, historical confirmation and the latest refit are distinct
objects. A selected historical protocol is not an unbiased historical selector.
"""
from __future__ import annotations

from contextlib import closing
from collections import Counter
import copy
from datetime import date, datetime
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

import numpy as np
import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from .dataset import DatasetSession, build_dataset, _read
from .models import check_stop, fit_predict, signal_metrics, validate_model_options
from .registry import ModelRegistry, canonical, digest


DEFAULTS = dict(train_windows=[756, 1260], valid_sessions=126, test_sessions=63,
                holdout_sessions=252, models=['ridge', 'lightgbm', 'mlp'],
                seeds=[42, 137, 2026], seed=42, seed_review_count=2,
                horizon=5, top_k=10, initial_cash=1_000_000.,
                max_total_weight=.8, max_stock_weight=.1, rebalance_sessions=5,
                backend='qlib', model_options=None, universe=None,
                include_margin=True, scope='all', symbols=[], worker_timeout_seconds=3600)
OBSERVED_PERIODS = [dict(start='2026-09-24', end='2026-10-22',
                         reason='Previously inspected experiment; not a new blind holdout')]
LIMITATIONS = [
    'Research cohort only; historical delisted population and corporate actions are not certified.',
    'The selected protocol was chosen after development evaluation. Its earlier reconstruction has protocol selection bias.',
    'Historical confirmation is never used for ranking; previously observed dates are not blind evidence.',
    'A latest refit has no independent out-of-sample performance and does not inherit its protocol returns.',
    'Raw-price labels quarantine large gaps; smaller unverified corporate actions remain a limitation.',
]


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(canonical(value), encoding='utf-8')
    temporary.replace(path)


def _canonical_table(table, schema, feature_columns):
    """Align names before casting; absent factors remain typed missing data."""
    extra = set(table.column_names)-set(schema.names)
    if extra:
        raise ValueError('Unexpected study partition columns: ' + ', '.join(sorted(extra)))
    arrays = []
    for field in schema:
        if field.name in table.column_names:
            arrays.append(table[field.name].cast(field.type))
        elif field.name in feature_columns:
            arrays.append(pa.nulls(table.num_rows, type=field.type))
        else:
            raise ValueError('Study partition is missing required column: ' + field.name)
    return pa.Table.from_arrays(arrays, schema=schema)


def _aggregate_reports(reports, symbols, universe_id, partitions, rows):
    """Aggregate independent batch counters without mutating the first batch."""
    result = copy.deepcopy(reports[0])
    if sum(item['rows'] for item in reports) != rows:
        raise ValueError('Feature batch report row counts do not match its partitions')
    result.update(rows=rows, symbols_requested=symbols, dynamic_universe=True,
                  partition_count=partitions, estimator_memory_policy='one_selected_fold_only', universe_id=universe_id)
    for key in ['symbols_with_bars', 'valid_bar_rows', 'feature_eligible_rows']:
        if all(key in item for item in reports):
            result[key] = sum(item[key] for item in reports)
        else:
            result.pop(key, None)
    if all('label_reason_counts' in item for item in reports):
        reasons = Counter()
        for item in reports:
            reasons.update(item['label_reason_counts'])
        result['label_reason_counts'] = dict(reasons)
    else:
        result.pop('label_reason_counts', None)
    coverage = [item.get('group_coverage') for item in reports]
    if any(item is not None for item in coverage):
        if any(item is None for item in coverage) or any(set(item) != set(coverage[0]) for item in coverage):
            raise ValueError('Feature batches have incomplete factor coverage metadata')
        for group, summary in result['group_coverage'].items():
            for key in ['nonempty_rows', 'complete_rows', 'nonempty_values']:
                summary[key] = sum(item[group][key] for item in coverage)
            summary['row_coverage'] = summary['nonempty_rows']/rows if rows else 0.
            summary['reason'] = 'observed_nonempty' if summary['nonempty_rows'] else 'no_published_available_values_in_window'
    return result


def _aggregate_sources(metadata, session_evidence):
    result = copy.deepcopy(metadata[0]['sources'])
    if any(item['sources'].get('read_ref') != result.get('read_ref') for item in metadata):
        raise ValueError('Feature batch source snapshots differ')
    evidence = [(name, item) for row in metadata for name, item in row['sources'].get('datasets', {}).items()]
    evidence += [(row['dataset'], row['evidence']) for row in session_evidence]
    if not evidence:
        return result
    datasets = {}
    for name, item in evidence:
        identity = {key: value for key, value in item.items() if key != 'files'}
        if name not in datasets:
            datasets[name] = dict(identity, files={})
        elif {key: value for key, value in datasets[name].items() if key != 'files'} != identity:
            raise ValueError('Feature source dataset identity differs between batches: ' + name)
        for entry in item.get('files', []):
            prior = datasets[name]['files'].get(entry['path'])
            if prior is not None and prior != entry:
                raise ValueError('Feature source file evidence differs between batches')
            datasets[name]['files'][entry['path']] = entry
    result['datasets'] = {name: dict(item, files=[item['files'][path] for path in sorted(item['files'])])
                          for name, item in datasets.items()}
    return result


def validate_spec(request):
    if not isinstance(request, dict):
        raise ValueError('Study parameters must be an object')
    extra = set(request) - set(DEFAULTS) - {'start', 'end', 'read_ref', 'resume_study_id', 'requested_end', 'universe_manifest'}
    if extra:
        raise ValueError('Unknown study parameters: ' + ', '.join(sorted(extra)))
    spec = {**DEFAULTS, **request}
    for key in ['start', 'end']:
        value = spec.get(key)
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError(key + ' must be an ISO date')
    if spec['start'] > spec['end']:
        raise ValueError('Study start must not exceed end')
    for key, low, high in [('valid_sessions', 10, 252), ('test_sessions', 10, 252),
                           ('holdout_sessions', 10, 504), ('horizon', 1, 20),
                           ('top_k', 1, 100), ('rebalance_sessions', 1, 252),
                           ('seed_review_count', 1, 4), ('worker_timeout_seconds', 60, 14400),
                           ('seed', 0, 2147483647)]:
        if isinstance(spec[key], bool) or not isinstance(spec[key], int) or not low <= spec[key] <= high:
            raise ValueError(f'{key} must be an integer in [{low},{high}]')
    windows = spec['train_windows']
    if not isinstance(windows, list) or not 1 <= len(windows) <= 4 or len(set(windows)) != len(windows) or any(
            isinstance(v, bool) or not isinstance(v, int) or not 40 <= v <= 2520 for v in windows):
        raise ValueError('train_windows must contain 1..4 distinct windows of 40..2520 sessions')
    spec['train_windows'] = sorted(windows)
    for key, choices in [('models', {'ridge', 'lightgbm', 'mlp'})]:
        values = spec[key]
        if not isinstance(values, list) or not values or len(set(values)) != len(values) or not set(values) <= choices:
            raise ValueError('Invalid study ' + key)
    seeds = spec['seeds']
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= 5 or len(set(seeds)) != len(seeds) or any(
            isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 2147483647 for v in seeds):
        raise ValueError('seeds must contain 1..5 distinct integers')
    if spec['seed'] not in seeds:
        raise ValueError('The fixed publication seed must be included in seeds')
    if spec['backend'] not in {'local', 'qlib'} or not isinstance(spec['include_margin'], bool):
        raise ValueError('Invalid backend or include_margin')
    for key in ['initial_cash', 'max_total_weight', 'max_stock_weight']:
        if isinstance(spec[key], bool) or not isinstance(spec[key], (float, int)) or not np.isfinite(spec[key]):
            raise ValueError('Invalid ' + key)
    if not 10000 <= spec['initial_cash'] <= 100000000 or not 0 < spec['max_stock_weight'] <= spec['max_total_weight'] <= 1:
        raise ValueError('Invalid study cash or portfolio weight caps')
    if spec.get('read_ref') is not None and not re.fullmatch('[0-9a-f]{64}', str(spec['read_ref'])):
        raise ValueError('Invalid study read_ref')
    if spec.get('resume_study_id') is not None and not re.fullmatch('study-[0-9a-f]{32}', str(spec['resume_study_id'])):
        raise ValueError('Invalid resume_study_id')
    if spec.get('universe_manifest') is not None and not isinstance(spec['universe_manifest'], str):
        raise ValueError('universe_manifest must be a saved manifest path')
    if spec['scope'] not in {'all', 'symbols'} or not isinstance(spec['symbols'], list):
        raise ValueError('Invalid study scope/symbols')
    if spec['scope'] == 'all' and spec['symbols']:
        raise ValueError('All-market studies cannot also specify symbols')
    if spec['scope'] == 'symbols' and (not spec['symbols'] or len(set(spec['symbols'])) != len(spec['symbols']) or any(
            not isinstance(v, str) or not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', v) for v in spec['symbols'])):
        raise ValueError('Study symbols must be distinct Shanghai/Shenzhen A-share identities')
    spec['model_options'] = validate_model_options(spec['model_options']) if 'mlp' in spec['models'] else None
    from .universe import UniversePolicy
    from dataclasses import asdict
    spec['universe'] = asdict(UniversePolicy.from_dict(spec['universe']))
    return spec


def study_folds(calendar, spec, train_sessions):
    """All common OOS dates, with an exact development/confirmation boundary."""
    days = sorted(day for day in set(calendar) if spec['start'] <= day <= spec['end'])
    first = max(spec['train_windows']) + spec['valid_sessions']
    split = len(days) - spec['holdout_sessions']
    if split - first < spec['test_sessions']:
        raise ValueError('Insufficient sessions for common training, development and historical confirmation')
    folds = []
    for segment, left, right in [('development', first, split), ('confirmation', split, len(days))]:
        for offset in range(left, right, spec['test_sessions']):
            valid = offset - spec['valid_sessions']
            folds.append(dict(fold=len(folds), train_start=days[valid-train_sessions], train_end=days[valid-1],
                valid_start=days[valid], valid_end=days[offset-1], test_start=days[offset],
                test_end=days[min(offset+spec['test_sessions'], right)-1], segment=segment))
    return folds


def selection_key(summary):
    """Deterministic lexicographic selection using development evidence only.

    Risk-adjusted return is primary after excess return: the 2x-cost Sharpe is
    compared before drawdown so a protocol cannot win on raw excess while
    carrying disproportionate volatility. A missing Sharpe ranks last.
    """
    value = summary['development']
    sharpe = (value.get('cost_stress_2x') or {}).get('sharpe')
    sharpe_key = -float(sharpe) if sharpe is not None else float('inf')
    return (-value['median_year_excess'], -value['stress_median_year_excess'],
            sharpe_key, value['max_drawdown'], -value['worst_year_excess'], value['turnover'],
            -value.get('rank_ic', 0.), summary['complexity'], summary['protocol_id'])


def _year_returns(portfolio):
    result, previous = {}, float(portfolio['metrics']['initial_cash'])
    for row in portfolio['daily']:
        year = str(row['date'])[:4]
        if year not in result:
            result[year] = [previous, float(row['equity'])]
        result[year][1] = float(row['equity'])
        previous = float(row['equity'])
    return {year: end/start-1 for year, (start, end) in result.items()}


class StudyService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.runs_root = self.root / 'var/learning/studies'
        self.index = self.root / 'var/learning/studies.db'

    def list_runs(self, limit=100):
        if not self.index.is_file():
            return {'studies': []}
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            return {'studies': [json.loads(row[0]) for row in conn.execute('SELECT summary FROM studies ORDER BY rowid DESC LIMIT ?', (-1 if limit is None else limit,))]}

    def read_run(self, study_id):
        if not isinstance(study_id, str) or not re.fullmatch('study-[0-9a-f]{32}', study_id):
            raise ValueError('Invalid study identity')
        if not self.index.is_file():
            raise KeyError(study_id)
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT summary FROM studies WHERE id=?', (study_id,)).fetchone()
        if row is None:
            raise KeyError(study_id)
        summary = json.loads(row[0])
        if summary['status'] != 'completed':
            return summary
        path = self.runs_root / study_id / 'report.json'
        if path.is_symlink() or digest(path) != summary['report_sha256']:
            raise ValueError('Study report SHA256 mismatch')
        return json.loads(path.read_text(encoding='utf-8'))

    def _index(self, summary):
        self.index.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.index, timeout=30)) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS studies (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
            conn.execute('INSERT INTO studies VALUES (?,?) ON CONFLICT(id) DO UPDATE SET summary=excluded.summary',
                         (summary['study_id'], canonical(summary)))
            conn.commit()

    def _calendar(self, spec):
        session = DatasetSession(self.root, spec.get('read_ref'))
        rows = _read(session.store, session.snapshot, 'trading_calendar', '1900-01-01', spec['end'], None, {}, None, session)
        days = sorted(pd.to_datetime(rows.loc[rows.is_trading.eq(True), 'trade_date']).dt.strftime('%Y-%m-%d'))
        evidence = session.source_files('daily_bars', spec['start'], spec['end'])
        if not evidence['files']:
            raise ValueError('No published daily bars in the study interval')
        latest = (pl.scan_parquet([row['path'] for row in evidence['files']], hive_partitioning=False)
                  .filter(pl.col('trade_date') <= date.fromisoformat(spec['end']))
                  .select(pl.col('trade_date').max()).collect().item())
        if latest is None:
            raise ValueError('No published daily bars at or before the requested cutoff')
        return [day for day in days if spec['start'] <= day <= min(spec['end'], str(latest)[:10])], session.snapshot['read_ref']

    def preflight(self, request):
        spec = validate_spec(request)
        days, read_ref = self._calendar(spec)
        if not days:
            raise ValueError('No verified complete trading sessions in the study range')
        spec = dict(spec, requested_end=spec.get('requested_end', spec['end']), end=days[-1])
        from .service import LearningService
        backend = next(row for row in LearningService(self.root).catalog()['backends'] if row['id'] == spec['backend'])
        blockers = [] if backend['available'] else [backend['reason']]
        plans = []
        for window in spec['train_windows']:
            try:
                folds = study_folds(days, spec, window)
                plans.append(dict(train_sessions=window, folds=folds, count=len(folds)))
            except ValueError as error:
                blockers.append(str(error))
        return dict(family='ten_year_study_preflight_v1', status='blocked' if blockers else 'ready',
            spec=spec, read_ref=read_ref, sessions=len(days), protocols=plans, blockers=sorted(set(blockers)),
            coverage_validation='Dynamic membership and mature labels are checked before every fit during run.',
            memory_policy='200-symbol feature batches; date-predicate reading of one selected fold; no ten-year concat',
            observed_periods=OBSERVED_PERIODS, qualification=self._qualification(), limitations=LIMITATIONS)

    @staticmethod
    def _qualification():
        return dict(research_only=True, strict_history_eligible=False, paper_allocation_eligible=False,
                    independent_forward_status='pending', model_stage='research_default_candidate')

    def _prepare(self, spec, working, progress, check_cancel):
        """Persist symbol-batch row groups; never assemble the ten-year panel."""
        from .universe import build_universe, load_membership, read_universe
        prepared = working / 'prepared.json'
        if prepared.is_file():
            result = json.loads(prepared.read_text(encoding='utf-8'))
            for row in result['partitions'] + [result['dataset_artifact']]:
                if digest(working / row['path']) != row['sha256']:
                    raise ValueError('Prepared study dataset SHA256 mismatch')
                if row.get('metadata_sha256') and digest((working / row['path']).with_suffix('.json')) != row['metadata_sha256']:
                    raise ValueError('Prepared study partition metadata SHA256 mismatch')
            metadata = [json.loads((working / row['path']).with_suffix('.json').read_text(encoding='utf-8'))['metadata']
                        for row in result['partitions']]
            expected = _aggregate_reports([row['report'] for row in metadata], result['symbols'],
                result['universe']['universe_id'], len(result['partitions']), sum(row['rows'] for row in result['partitions']))
            if result['report'] != expected or result['sources'] != _aggregate_sources(metadata, result.get('source_session_evidence', [])):
                raise ValueError('Prepared study aggregate coverage or source metadata mismatch')
            return result
        session = DatasetSession(self.root, spec['read_ref'], check_cancel)
        # Manifests contain absolute paths. Keep them outside the build directory
        # that is atomically renamed on publication.
        inputs = self.runs_root / 'inputs' / working.name.removeprefix('.').removesuffix('.building')
        if spec.get('universe_manifest'):
            universe = read_universe(spec['universe_manifest'])
            if universe['read_ref'] != spec['read_ref'] or universe['policy'] != spec['universe'] or universe.get('start') != spec['start'] or universe.get('end') != spec['end']:
                raise ValueError('Saved universe read, policy or date interval differs from the study')
        else:
            retained = sorted(inputs.glob('universe*/manifest.json')) if inputs.is_dir() else []
            if retained:
                universe = read_universe(retained[0])
                if universe['read_ref'] != spec['read_ref'] or universe['policy'] != spec['universe'] or universe['start'] != spec['start'] or universe['end'] != spec['end']:
                    raise ValueError('Resumed universe identity differs from the frozen study')
            else:
                destination = inputs / 'universe'
                if destination.exists():
                    # Preserve interrupted evidence; a new isolated attempt is
                    # preferable to deleting a possibly useful audit directory.
                    destination = inputs / ('universe-attempt-'+uuid.uuid4().hex)
                universe = build_universe(self.root, spec['start'], spec['end'], destination,
                    policy=spec['universe'], session=session, check_cancel=check_cancel,
                    progress=lambda **value: progress(value) if progress else None)
        symbols = sorted(set(universe['symbols']) & set(spec['symbols'])) if spec['scope'] == 'symbols' else universe['symbols']
        if not symbols:
            raise ValueError('No symbols satisfy the frozen dynamic universe policy')
        membership = load_membership(universe)[['date', 'symbol']]
        parts, reference, rows, batch_metadata = [], None, 0, []
        (working / 'partitions').mkdir(exist_ok=True)
        for offset in range(0, len(symbols), 200):
            check_stop(check_cancel)
            batch = symbols[offset:offset+200]
            path = working / 'partitions' / f'batch-{offset//200:04d}.parquet'
            metadata = path.with_suffix('.json')
            if metadata.is_file():
                record = json.loads(metadata.read_text(encoding='utf-8'))
                if digest(path) != record['sha256'] or record['symbols'] != batch:
                    raise ValueError('Cached feature partition identity mismatch')
                data_meta = record['metadata']
            else:
                data = build_dataset(self.root, dict(symbols=batch, start=spec['start'], end=spec['end'],
                    factor_groups=['price_volume', 'margin'] if spec['include_margin'] else ['price_volume'],
                    horizon=spec['horizon'], read_ref=spec['read_ref']), session=session, check_cancel=check_cancel)
                keys = membership.loc[membership.symbol.isin(batch)].assign(universe_eligible=True)
                frame = data['frame'].merge(keys, on=['date', 'symbol'], how='left', validate='one_to_one')
                frame['universe_eligible'] = frame.universe_eligible.eq(True)
                frame.to_parquet(path, index=False, row_group_size=25000)
                data_meta = {key: value for key, value in data.items() if key != 'frame'}
                record = dict(path=path.relative_to(working).as_posix(), sha256=digest(path), rows=len(frame), symbols=batch, metadata=data_meta)
                _write(metadata, record)
                del data, frame
            reference = reference or copy.deepcopy(data_meta)
            batch_metadata.append(data_meta)
            parts.append(dict({key: record[key] for key in ['path', 'sha256', 'rows']}, metadata_sha256=digest(metadata)))
            rows += record['rows']
            if progress:
                progress(dict(phase='study_features', current=min(offset+200, len(symbols)), total=len(symbols)))
        session.verify()
        target = working / 'dataset.parquet'
        writer = None
        feature_columns = {column for columns in reference['feature_groups'].values() for column in columns}
        try:
            for item in parts:
                for batch in pq.ParquetFile(working / item['path']).iter_batches(batch_size=25000):
                    table = pa.Table.from_batches([batch])
                    if writer is None:
                        fields = {field.name: field for field in table.schema}
                        for column in feature_columns:
                            if column not in fields or pa.types.is_null(fields[column].type):
                                fields[column] = pa.field(column, pa.float64())
                        schema = pa.schema([fields[name] for name in sorted(fields)])
                        writer = pq.ParquetWriter(target, schema, compression='zstd')
                    writer.write_table(_canonical_table(table, writer.schema, feature_columns))
        finally:
            if writer is not None:
                writer.close()
        reference['report'] = _aggregate_reports([row['report'] for row in batch_metadata], symbols,
                                                universe['universe_id'], len(parts), rows)
        session_evidence = [dict(dataset=key[0], evidence=value) for key, value in getattr(session, 'evidence', {}).items()]
        reference['sources'] = _aggregate_sources(batch_metadata, session_evidence)
        result = dict(**reference, universe=universe, symbols=symbols, partitions=parts,
                      source_session_evidence=session_evidence,
                      dataset_columns=writer.schema.names,
                      dataset_artifact=dict(path='dataset.parquet', sha256=digest(target), size_bytes=target.stat().st_size))
        _write(prepared, result)
        return result

    @staticmethod
    def _frame(working, prepared, start, end, columns=None):
        query = pl.scan_parquet(str(working / 'dataset.parquet'), hive_partitioning=False)
        query = query.filter(pl.col('date').is_between(pl.lit(start), pl.lit(end)))
        if columns:
            available = query.collect_schema().names()
            query = query.select([column for column in columns if column in available])
        return query.collect().to_pandas()

    @staticmethod
    def _config(spec, **overrides):
        return {**{key: spec[key] for key in ['top_k', 'initial_cash', 'max_total_weight', 'max_stock_weight', 'rebalance_sessions']}, **overrides}

    def _portfolio(self, working, prepared, scores, calendar, spec, name, check_cancel, stress=1.):
        from .portfolio import backtest_scores
        from .execution import PRICE_COLUMNS, EXECUTION_EVIDENCE_COLUMNS
        # Only names ever in a target can acquire inventory. Other names still
        # remain in the immutable dataset for audits and alternative replays.
        targets = scores.sort_values(['date', 'score', 'symbol'], ascending=[True, False, True]).groupby('date', sort=False).head(spec['top_k'])
        symbols = targets.symbol.unique().tolist()
        first, last = str(scores.date.min()), str(scores.date.max())
        prior = [day for day in calendar if day < first]
        prefix = prior[-1] if prior else first
        query = pl.scan_parquet(str(working / 'dataset.parquet'), hive_partitioning=False)
        query = query.filter(pl.col('date').is_between(pl.lit(prefix), pl.lit(last)) & pl.col('symbol').is_in(symbols))
        fields = query.collect_schema().names()
        prices = query.select([c for c in (*PRICE_COLUMNS, *EXECUTION_EVIDENCE_COLUMNS) if c in fields]).collect().to_pandas()
        evaluation_calendar = [day for day in calendar if day <= last]
        return backtest_scores(prices, scores, evaluation_calendar, self._config(spec, cost_stress=stress), run_id=name, check_cancel=check_cancel)

    def _benchmark(self, working, prepared, selected, days, spec, name, check_cancel):
        scores = selected.copy()
        scores['score'] = 0.
        config = dict(spec, top_k=spec['universe']['max_members'])
        keys = scores[['date', 'symbol']].sort_values(['date', 'symbol'])
        version = hashlib.sha256(pd.util.hash_pandas_object(keys, index=False).to_numpy().tobytes()
            + canonical(self._config(config)).encode()).hexdigest()
        cache = working / 'benchmarks' / (version+'.json')
        checksum = cache.with_suffix('.sha256')
        if cache.is_file() and checksum.is_file():
            if digest(cache) != checksum.read_text(encoding='ascii'):
                raise ValueError('Cached common benchmark SHA256 mismatch')
            return json.loads(cache.read_text(encoding='utf-8'))
        cache.parent.mkdir(exist_ok=True)
        result = self._portfolio(working, prepared, scores, days, config, name, check_cancel)
        _write(cache, result)
        checksum.write_text(digest(cache), encoding='ascii')
        return result

    def _fit_protocol(self, spec, protocol, prepared, working, run_id, progress, check_cancel, segment='all', folds_override=None):
        """Checkpoint one protocol/fold; publish only complete immutable reports."""
        from .service import LearningService
        service = LearningService(self.root)
        completed = service.runs_root / run_id
        if completed.is_dir():
            return service.read_run(run_id)
        folder = service.runs_root / ('.' + run_id + '.building')
        folder.mkdir(parents=True, exist_ok=True)
        days = [day for day in prepared['calendar'] if spec['start'] <= day <= spec['end']]
        folds = folds_override if folds_override is not None else study_folds(days, spec, protocol['train_sessions'])
        if protocol.get('minimum_train_start'):
            folds = [row for row in folds if row['train_start'] >= protocol['minimum_train_start']]
        if segment != 'all':
            folds = [row for row in folds if row['segment'] == segment]
        if not folds:
            raise ValueError('Protocol has no valid common out-of-sample folds')
        groups = prepared['feature_groups']
        features = list(dict.fromkeys(column for group in protocol['factor_groups'] for column in groups[group]))
        model_name = protocol['model_name']
        predictions, details_rows, finished_folds = [], [], []
        for number, original in enumerate(folds):
            check_stop(check_cancel)
            fold = dict(original)
            cache = folder / f"fold-{fold['fold']}.json"
            scores_path = folder / f"fold-{fold['fold']}.parquet"
            if cache.is_file():
                saved = json.loads(cache.read_text(encoding='utf-8'))
                for path, sha in saved['artifacts'].items():
                    if digest(folder / path) != sha:
                        raise ValueError('Resumed model fold SHA256 mismatch')
                out, row, fold = pd.read_parquet(scores_path), saved['details'], saved['fold']
            else:
                frame = self._frame(working, prepared, fold['train_start'], fold['test_end'])
                eligible = frame.universe_eligible.eq(True) & frame.feature_eligible.eq(True) & frame.legacy_ma_eligible.eq(True)
                train_mask = eligible & frame.date.between(fold['train_start'], fold['train_end']) & frame.label.notna()
                valid_mask = eligible & frame.date.between(fold['valid_start'], fold['valid_end']) & frame.label.notna()
                train = frame.loc[train_mask & frame.label_end.lt(fold['valid_start']).fillna(False)]
                valid = frame.loc[valid_mask & frame.label_end.lt(fold['test_start']).fillna(False)]
                test = frame.loc[eligible & frame.date.between(fold['test_start'], fold['test_end'])]
                if len(train) < 20 or len(valid) < 10 or test.empty:
                    raise ValueError(f"Insufficient dynamic pool observations in fold {fold['fold']}")
                for group in protocol['factor_groups']:
                    if not train[groups[group]].notna().any().any():
                        raise ValueError('Requested training group has no real observations: ' + group)
                cutoff = max(train.label_end.max(), valid.label_end.max())
                if cutoff >= fold['test_start']:
                    raise ValueError('Study labels cross the evaluation boundary')
                destination = folder / 'models' / f"fold-{fold['fold']}" / model_name
                if destination.exists():
                    # Only a partial, unpublished model folder can be replaced.
                    if not destination.resolve().is_relative_to(folder.resolve()):
                        raise ValueError('Partial model escaped the study build directory')
                    shutil.rmtree(destination)
                values, details = fit_predict(train, valid, test, features, model_type=protocol['model_type'],
                    backend=spec['backend'], seed=protocol['seed'], directory=destination, root=self.root,
                    model_options=spec['model_options'] if protocol['model_type'] == 'mlp' else None,
                    worker_timeout_seconds=spec['worker_timeout_seconds'], check_cancel=check_cancel)
                out = test[['date', 'symbol', 'label', 'label_end']].copy()
                out['score'], out['model_name'], out['fold'] = values, model_name, fold['fold']
                out['train_end'], out['fit_train_end'] = cutoff, fold['train_end']
                out['available_at'] = out.date + 'T15:00:00+08:00'
                out['input_id'], out['model_version'] = spec['read_ref'], digest(destination / 'model.json')
                out['feature_schema_sha256'] = hashlib.sha256(canonical(details['preprocessing']['columns']).encode()).hexdigest()
                fold.update(fit_end=cutoff, train_rows=len(train), valid_rows=len(valid), test_rows=len(test),
                    purged_train_rows=int(train_mask.sum()-len(train)), purged_valid_rows=int(valid_mask.sum()-len(valid)))
                row = dict(name=model_name, fold=fold['fold'], feature_set=protocol['feature_set'],
                    feature_count=details['feature_count'], columns=details['preprocessing']['columns'],
                    engine=details['engine'], importance=details.get('importance', {}),
                    dropped_training_empty=details['preprocessing']['dropped_training_empty'])
                out.to_parquet(scores_path, index=False)
                artifacts = {path.relative_to(folder).as_posix(): digest(path) for path in destination.iterdir() if path.is_file()}
                artifacts[scores_path.name] = digest(scores_path)
                _write(cache, dict(fold=fold, details=row, artifacts=artifacts))
                del frame, train, valid, test
                gc.collect()
            predictions.append(out)
            details_rows.append(row)
            finished_folds.append(fold)
            if progress:
                progress(dict(phase='study_fold', protocol_id=protocol['protocol_id'], seed=protocol['seed'],
                              current=number+1, total=len(folds), fold=fold['fold'], segment=fold['segment']))
        oos = pd.concat(predictions, ignore_index=True).sort_values(['date', 'symbol']).reset_index(drop=True)
        if oos.duplicated(['date', 'symbol']).any():
            raise ValueError('Overlapping study test intervals')
        oos.to_parquet(folder / 'predictions.parquet', index=False)
        evaluations = {}
        for label in ['development', 'confirmation', 'all']:
            selected_folds = [row for row in finished_folds if label == 'all' or row['segment'] == label]
            if not selected_folds:
                continue
            selected = oos.loc[oos.date.between(selected_folds[0]['test_start'], selected_folds[-1]['test_end'])]
            portfolio = self._portfolio(working, prepared, selected, days, spec, run_id+'-'+label, check_cancel)
            stress = self._portfolio(working, prepared, selected, days, spec, run_id+'-'+label+'-cost2', check_cancel, 2.)
            # The benchmark uses the same entire dynamic population. It is an
            # equal target-weight research account, not a synthetic index.
            benchmark = self._benchmark(working, prepared, selected, days, spec, run_id+'-'+label+'-equal', check_cancel)
            years, bench_years, stress_years = _year_returns(portfolio), _year_returns(benchmark), _year_returns(stress)
            excess = {year: result-bench_years[year] for year, result in years.items()}
            stress_excess = {year: result-bench_years[year] for year, result in stress_years.items()}
            metrics = signal_metrics(selected)
            evaluations[label] = dict(portfolio=portfolio, cost_stress_2x=stress['metrics'], benchmark=benchmark,
                annual_returns=years, annual_excess=excess, median_year_excess=float(np.median(list(excess.values()))),
                stress_median_year_excess=float(np.median(list(stress_excess.values()))),
                worst_year_excess=float(min(excess.values())), max_drawdown=portfolio['metrics']['max_drawdown'],
                turnover=portfolio['metrics']['turnover'], rank_ic=float(metrics.get('rank_ic') or 0.), signal_metrics=metrics)
        if 'all' not in evaluations:
            raise ValueError('No portfolio evaluation produced')
        shared = folder / 'dataset.parquet'
        if not shared.exists():
            os.link(working / 'dataset.parquet', shared)
        if digest(shared) != prepared['dataset_artifact']['sha256']:
            raise ValueError('Shared immutable dataset identity mismatch')
        current = evaluations['all']
        experiment = dict(name=model_name, model_type=protocol['model_type'], feature_set=protocol['feature_set'],
            requested_feature_count=len(features), feature_count=details_rows[-1]['feature_count'],
            feature_count_range=[min(row['feature_count'] for row in details_rows), max(row['feature_count'] for row in details_rows)],
            metrics=current['signal_metrics'], portfolio=current['portfolio'], fold_models=details_rows,
            cost_stress_2x=current['cost_stress_2x'],
            excess_return_vs_equal_weight=current['portfolio']['metrics']['total_return']-current['benchmark']['metrics']['total_return'])
        report = dict(family='cross_sectional_return_v1', purpose='ten_year_protocol', run_id=run_id, lake_run_id=run_id,
            status='completed', created_at=datetime.now().astimezone().isoformat(),
            spec=dict(spec, symbols=prepared['symbols'], train_sessions=protocol['train_sessions'], factor_groups=protocol['factor_groups'], seed=protocol['seed']),
            protocol=protocol, dataset=prepared['report'], sources=prepared['sources'], feature_groups=groups,
            universe=prepared['universe'], selected_factor_groups=protocol['factor_groups'], folds=finished_folds,
            models=[experiment], evaluations=evaluations, benchmark=current['benchmark'],
            implementation_sha256=self._code_identity(), qualification=self._qualification(), limitations=LIMITATIONS)
        report['artifacts'] = {path.relative_to(folder).as_posix(): dict(path=path.relative_to(folder).as_posix(), sha256=digest(path), size_bytes=path.stat().st_size)
                               for path in folder.rglob('*') if path.is_file()}
        _write(folder / 'report.json', report)
        check_stop(check_cancel)
        folder.rename(completed)
        ModelRegistry(self.root).publish_run(report)
        return report

    def historical_backtest(self, params, progress=None, check_cancel=None):
        """Reconstruct earlier folds from an unchanged parent input contract."""
        from datetime import date
        import hashlib
        import json
        import os
        import numpy as np
        import pandas as pd
        from .dataset import DatasetSession, _read
        from .models import check_stop
        from .registry import ModelRegistry, canonical, digest
        from .service import LearningService
        from .workflow import LearningWorkflow
        from .study import _write

        if params.get('scope') is not None or params.get('symbols'):
            raise ValueError('Historical protocol reconstruction uses its frozen dynamic universe; scope/symbol overrides are not supported')
        parent = self.read_run(params.get('study_id'))
        if parent.get('status') != 'completed':
            raise ValueError('Historical reconstruction requires a completed frozen study')
        start, end = params.get('start'), params.get('end')
        for value in [start, end]:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError('Historical start/end must be ISO dates')
        if start > end:
            raise ValueError('Historical start must not exceed end')
        if params.get('read_ref') not in {None, parent['read_ref']}:
            raise ValueError('A different historical read_ref requires a new explicit study input contract')
        original, protocol = parent['spec'], dict(parent['selected_protocol'])
        parent_folder = self.runs_root/parent['study_id']
        prepared = json.loads((parent_folder/'prepared.json').read_text(encoding='utf-8'))
        source_dataset = parent_folder/'dataset.parquet'
        if (prepared['sources']['read_ref'] != parent['read_ref'] or prepared['report']['start'] != original['start']
                or digest(source_dataset) != prepared['dataset_artifact']['sha256']):
            raise ValueError('Frozen parent historical feature input identity mismatch')
        source = LearningService(self.root).read_run(parent['best_run_id'])
        if source['artifacts']['dataset.parquet']['sha256'] != prepared['dataset_artifact']['sha256']:
            raise ValueError('Parent protocol and prepared dataset identities differ')
        session = DatasetSession(self.root, parent['read_ref'], check_cancel)
        calendar_frame = _read(session.store, session.snapshot, 'trading_calendar', '1900-01-01', max(end, original['end']),
                               None, {}, check_cancel, session)
        session.verify()
        days = sorted(pd.to_datetime(calendar_frame.loc[calendar_frame.is_trading.eq(True), 'trade_date']).dt.strftime('%Y-%m-%d'))
        requested = [i for i, day in enumerate(days) if start <= day <= end]
        if not requested:
            raise ValueError('Requested historical interval contains no verified trading session')
        if days[requested[-1]] > original['end']:
            raise ValueError('Historical interval exceeds the frozen parent input; publish a new explicit study input contract')
        anchor = int(np.searchsorted(days, source['folds'][0]['test_start']))
        first_input = int(np.searchsorted(days, original['start']))
        stride = original['test_sessions']
        minimum_test = first_input+original['valid_sessions']+protocol['train_sessions']
        earliest = anchor-((anchor-minimum_test)//stride)*stride
        if requested[0] < earliest:
            raise ValueError('Insufficient frozen parent training history; earliest reconstructible signal is '+days[earliest]
                             +'. Earlier membership and features require a new audited input contract.')
        first = anchor+((requested[0]-anchor)//stride)*stride
        folds = []
        # Extend the original first-OOS anchor backwards. Never extend that grid
        # through the independent historical-confirmation boundary.
        for offset in range(first, min(anchor, requested[-1]+1), stride):
            valid = offset-original['valid_sessions']
            folds.append(dict(fold=len(folds), train_start=days[valid-protocol['train_sessions']], train_end=days[valid-1],
                valid_start=days[valid], valid_end=days[offset-1], test_start=days[offset],
                test_end=days[min(offset+stride-1, requested[-1])], segment='historical'))
        for original_fold in source['folds']:
            if original_fold['test_end'] < days[requested[0]] or original_fold['test_start'] > days[requested[-1]]:
                continue
            keys = ['train_start', 'train_end', 'valid_start', 'valid_end', 'test_start', 'test_end']
            fold = {key: original_fold[key] for key in keys}
            fold.update(fold=len(folds), segment='historical', source_schedule_fold=original_fold['fold'])
            fold['test_end'] = min(fold['test_end'], days[requested[-1]])
            folds.append(fold)
        if not folds:
            raise ValueError('No historical folds are supported by the frozen parent input')
        spec = dict(original)
        for key in ['top_k', 'initial_cash', 'rebalance_sessions', 'max_total_weight', 'max_stock_weight']:
            if key in params:
                spec[key] = params[key]
        input_identity = dict(parent_study_id=parent['study_id'], parent_run_id=parent['best_run_id'],
            parent_dataset_sha256=prepared['dataset_artifact']['sha256'], read_ref=parent['read_ref'],
            feature_history_start=original['start'], universe_id=prepared['universe']['universe_id'],
            population_order_sha256=hashlib.sha256(canonical(prepared['symbols']).encode()).hexdigest(),
            parent_implementation_sha256=parent['implementation_sha256'],
            actual_signal_start=days[requested[0]], actual_signal_end=days[requested[-1]],
            requested_start=start, requested_end=end,
            label_evaluation_cutoff=original['end'])
        protocol['historical_input_identity'] = input_identity
        identity = dict(study_id=parent['study_id'], protocol=protocol, spec=spec, folds=folds,
                        implementation_sha256=self._code_identity())
        key = hashlib.sha256(canonical(identity).encode()).hexdigest()
        working = self.runs_root/'historical'/key
        working.mkdir(parents=True, exist_ok=True)
        metadata = working/'reconstruction.json'
        if metadata.is_file() and json.loads(metadata.read_text(encoding='utf-8')) != identity:
            raise ValueError('Historical reconstruction cache identity mismatch')
        _write(metadata, identity)
        target = working/'dataset.parquet'
        if not target.exists():
            os.link(source_dataset, target)
        if digest(target) != prepared['dataset_artifact']['sha256']:
            raise ValueError('Historical dataset link differs from the frozen parent')
        check_stop(check_cancel)
        evaluated = self._fit_protocol(spec, protocol, prepared, working, 'learn-'+key[:32], progress, check_cancel,
                                       folds_override=folds)
        series = [row for row in ModelRegistry(self.root).list_models()['series'] if row['run_id'] == evaluated['run_id']]
        if len(series) != 1:
            raise ValueError('Historical model series publication is incomplete')
        request = {key: value for key, value in params.items() if key in {
            'top_k', 'initial_cash', 'rebalance_sessions', 'max_total_weight', 'max_stock_weight',
            'cost_stress', 'require_verified_rules', 'release_id'}}
        request.update(mode='rolling', series_id=series[0]['series_id'], start=days[requested[0]], end=days[requested[-1]],
            reconstruction_requested_start=start, reconstruction_requested_end=end,
            historical_protocol_selection_bias=True, parent_study_id=parent['study_id'])
        return LearningWorkflow(self.root).backtest(request, progress=progress, check_cancel=check_cancel)

    @staticmethod
    def _code_identity():
        names = ['study.py', 'dataset.py', 'features.py', 'models.py', 'neural.py', 'qlib_worker.py',
                 'portfolio.py', 'execution.py', 'universe.py', 'registry.py', 'service.py']
        result = {name: digest(Path(__file__).with_name(name)) for name in names}
        from .portfolio import _implementation
        result.update({'execution_dependency/'+key: value for key, value in _implementation().items()})
        return result

    def _summary(self, report):
        protocol = report['protocol']
        return dict(protocol_id=protocol['protocol_id'], protocol=protocol, run_id=report['run_id'],
            complexity={'ridge': 0, 'lightgbm': 1, 'mlp': 2}[protocol['model_type']],
            development={key: value for key, value in report['evaluations']['development'].items() if key not in {'portfolio', 'benchmark'}},
            confirmation={key: value for key, value in report['evaluations'].get('confirmation', {}).items() if key not in {'portfolio', 'benchmark'}})

    def _refit(self, spec, protocol, parent, prepared, working, run_id, progress, check_cancel):
        """Latest-window weights are independent of all historical scores."""
        from .service import LearningService
        service = LearningService(self.root)
        if (service.runs_root / run_id).is_dir():
            return service.read_run(run_id)
        folder = service.runs_root / ('.' + run_id + '.building')
        folder.mkdir(parents=True, exist_ok=True)
        days = [day for day in prepared['calendar'] if spec['start'] <= day <= spec['end']]
        # Pick the last fully mature label date, requiring label_end strictly
        # before the inference cutoff, exactly as the fixed-model API does.
        mature_last = len(days)-spec['horizon']-2
        valid_start = mature_last-spec['valid_sessions']+1
        train_start = valid_start-protocol['train_sessions']
        if train_start < 0:
            raise ValueError('Insufficient mature history for final study refit')
        frame = self._frame(working, prepared, days[train_start], days[-1])
        eligible = frame.universe_eligible.eq(True) & frame.feature_eligible.eq(True) & frame.legacy_ma_eligible.eq(True)
        mature = eligible & frame.label.notna() & frame.label_end.lt(days[-1]).fillna(False)
        train = frame.loc[mature & frame.date.between(days[train_start], days[valid_start-1]) & frame.label_end.lt(days[valid_start]).fillna(False)]
        valid = frame.loc[mature & frame.date.between(days[valid_start], days[mature_last])]
        test = frame.loc[eligible & frame.date.eq(days[-1])]
        if len(train) < 20 or len(valid) < 10 or test.empty:
            raise ValueError('Insufficient current dynamic membership or mature labels for refit')
        groups = prepared['feature_groups']
        columns = list(dict.fromkeys(c for group in protocol['factor_groups'] for c in groups[group]))
        destination = folder / 'models/fold-0' / protocol['model_name']
        if destination.exists():
            if not destination.resolve().is_relative_to(folder.resolve()):
                raise ValueError('Partial refit escaped build directory')
            shutil.rmtree(destination)
        if progress:
            progress(dict(phase='study_final_refit', protocol_id=protocol['protocol_id'], train_rows=len(train), valid_rows=len(valid)))
        _, details = fit_predict(train, valid, test, columns, model_type=protocol['model_type'], backend=spec['backend'],
            seed=spec['seed'], root=self.root, directory=destination, check_cancel=check_cancel,
            model_options=spec['model_options'] if protocol['model_type'] == 'mlp' else None,
            worker_timeout_seconds=spec['worker_timeout_seconds'])
        cutoff = max(train.label_end.max(), valid.label_end.max())
        fold = dict(fold=0, train_start=days[train_start], train_end=days[valid_start-1], valid_start=days[valid_start],
            valid_end=days[mature_last], test_start=None, test_end=None, fit_end=cutoff, train_rows=len(train), valid_rows=len(valid), test_rows=0)
        frame.to_parquet(folder / 'dataset.parquet', index=False)
        report = dict(family='frozen_factor_refit_v1', purpose='final_refit', run_id=run_id, lake_run_id=run_id,
            status='completed', created_at=datetime.now().astimezone().isoformat(), parent_run_id=parent['run_id'],
            spec=dict(spec, symbols=prepared['symbols'], seed=spec['seed'], train_sessions=protocol['train_sessions'], factor_groups=protocol['factor_groups']),
            fixed_protocol_model=protocol['model_name'], protocol=protocol, dataset=prepared['report'], sources=prepared['sources'],
            feature_groups=groups, selected_factor_groups=protocol['factor_groups'], folds=[fold], universe=prepared['universe'],
            models=[dict(name=protocol['model_name'], model_type=protocol['model_type'], feature_set=protocol['feature_set'],
                feature_count=details['feature_count'], metrics={}, portfolio={}, fold_models=[], cost_stress_2x={})],
            benchmark={}, qualification=self._qualification(), implementation_sha256=self._code_identity(), limitations=LIMITATIONS)
        report['artifacts'] = {path.relative_to(folder).as_posix(): dict(path=path.relative_to(folder).as_posix(), sha256=digest(path), size_bytes=path.stat().st_size)
                               for path in folder.rglob('*') if path.is_file()}
        _write(folder / 'report.json', report)
        check_stop(check_cancel)
        folder.rename(service.runs_root / run_id)
        ModelRegistry(self.root).publish_run(report)
        return report

    def run(self, request, progress=None, check_cancel=None):
        if isinstance(request, dict) and re.fullmatch('study-[0-9a-f]{32}', str(request.get('resume_study_id', ''))):
            checkpoint = self.runs_root / ('.' + request['resume_study_id'] + '.building') / 'checkpoint.json'
            if checkpoint.is_file():
                request = {**json.loads(checkpoint.read_text(encoding='utf-8'))['spec'], **request}
        spec = validate_spec(request)
        resumed = spec.pop('resume_study_id', None)
        study_id = resumed or 'study-' + uuid.uuid4().hex
        if resumed and (self.runs_root / study_id).is_dir():
            return self.read_run(study_id)
        working = self.runs_root / ('.' + study_id + '.building')
        if resumed and not working.is_dir():
            raise ValueError('No resumable study checkpoint exists')
        preflight = self.preflight(spec)
        if preflight['status'] != 'ready':
            raise ValueError('Study preflight blocked: ' + '; '.join(preflight['blockers']))
        spec = dict(preflight['spec'], read_ref=preflight['read_ref'])
        working.mkdir(parents=True, exist_ok=True)
        lock = working / '.running'
        try:
            lock.mkdir()
        except FileExistsError:
            import psutil
            owner_path = lock / 'owner.json'
            owner = json.loads(owner_path.read_text(encoding='utf-8')) if owner_path.is_file() else {}
            live = owner.get('pid') and psutil.pid_exists(owner['pid'])
            if live:
                try:
                    live = abs(psutil.Process(owner['pid']).create_time()-owner['process_created_at']) < .01
                except psutil.NoSuchProcess:
                    live = False
            if live or not owner:
                raise ValueError('Study already has an active writer; inspect its owning job before resuming') from None
            owner_path.unlink()
            lock.rmdir()
            lock.mkdir()
        import psutil
        _write(lock / 'owner.json', dict(pid=os.getpid(), process_created_at=psutil.Process().create_time()))
        state_path = working / 'checkpoint.json'
        identity = self._code_identity()
        fingerprint = hashlib.sha256(canonical(spec).encode()).hexdigest()
        try:
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding='utf-8'))
                if state['spec_sha256'] != fingerprint or state['implementation_sha256'] != identity:
                    raise ValueError('Resume parameters or learning implementation differ from the original study')
                state['status'] = 'running'
                state.pop('error', None)
                _write(state_path, state)
            else:
                state = dict(study_id=study_id, spec=spec, spec_sha256=fingerprint, implementation_sha256=identity,
                    created_at=datetime.now().astimezone().isoformat(), runs={}, completed=[], status='running')
                _write(state_path, state)
            self._index({key: state[key] for key in ['study_id', 'created_at', 'spec', 'status']})
            if progress:
                progress(dict(phase='study_started', study_id=study_id, resumable=True))
            prepared = self._prepare(spec, working, progress, check_cancel)
            protocols = []
            for window in spec['train_windows']:
                protocols.append(dict(protocol_id=f'ma20-ridge-w{window}', model_name='legacy_ma_ridge', model_type='ridge',
                    feature_set='legacy_ma', factor_groups=['legacy_ma'], train_sessions=window, seed=spec['seed']))
                for model in spec['models']:
                    protocols.append(dict(protocol_id=f'pv38-{model}-w{window}', model_name='price_volume_'+model, model_type=model,
                        feature_set='price_volume', factor_groups=['price_volume'], train_sessions=window, seed=spec['seed']))
            reports, summaries = {}, []

            def fit(protocol, *, segment='all'):
                key = protocol['protocol_id'] + '-seed' + str(protocol['seed']) + '-' + segment
                if key not in state['runs']:
                    state['runs'][key] = 'learn-' + uuid.uuid4().hex
                    _write(state_path, state)
                result = self._fit_protocol(spec, protocol, prepared, working, state['runs'][key], progress, check_cancel, segment)
                if key not in state['completed']:
                    state['completed'].append(key)
                    _write(state_path, state)
                return result

            for protocol in protocols:
                check_stop(check_cancel)
                report = fit(protocol)
                reports[protocol['protocol_id']] = report
                summaries.append(self._summary(report))
            ranked = sorted(summaries, key=selection_key)
            # The publication seed is fixed in advance. Additional seeds measure
            # instability; no lucky seed ever replaces the publication weights.
            seed_reviews = []
            for summary in ranked[:spec['seed_review_count']]:
                for seed in spec['seeds']:
                    if seed == spec['seed']:
                        continue
                    protocol = dict(summary['protocol'], seed=seed)
                    seed_report = fit(protocol, segment='development')
                    seed_reviews.append(self._summary(seed_report))
            reviewed = []
            for base in ranked[:spec['seed_review_count']]:
                observations = [base]+[row for row in seed_reviews if row['protocol_id'] == base['protocol_id']]
                aggregate = dict(base)
                aggregate['development'] = dict(base['development'])
                for metric in ['median_year_excess', 'stress_median_year_excess', 'turnover', 'rank_ic']:
                    aggregate['development'][metric] = float(np.median([row['development'][metric] for row in observations]))
                aggregate['development']['max_drawdown'] = max(row['development']['max_drawdown'] for row in observations)
                aggregate['development']['worst_year_excess'] = min(row['development']['worst_year_excess'] for row in observations)
                aggregate['seeds_reviewed'] = [row['protocol']['seed'] for row in observations]
                aggregate['seed_median_excess_std'] = float(np.std([row['development']['median_year_excess'] for row in observations]))
                reviewed.append(aggregate)
            reviewed.sort(key=selection_key)
            winner = reviewed[0]
            selected = winner['protocol']
            enhancements = []
            if spec['include_margin'] and prepared['feature_groups'].get('margin'):
                # Margin is a paired recent-window study, never a shorter series
                # compared directly against the long main-line leaderboard.
                margin = dict(selected, protocol_id=selected['protocol_id']+'-margin', model_name='factor_'+selected['model_type'],
                    feature_set='all_factors', factor_groups=['price_volume', 'margin'], minimum_train_start='2021-01-04')
                valid = [row for row in study_folds(prepared['calendar'], spec, selected['train_sessions']) if row['train_start'] >= margin['minimum_train_start']]
                if any(row['segment'] == 'development' for row in valid):
                    enhanced = fit(margin)
                    enhancements.append(dict(run_id=enhanced['run_id'], protocol=margin, status='paired_research_only',
                        evaluations=enhanced['evaluations'], comparable_main_run_id=reports[selected['protocol_id']]['run_id'],
                        comparison_start=valid[0]['test_start'], automatically_selected=False))
                else:
                    enhancements.append(dict(protocol=margin, status='insufficient_common_development_history', automatically_selected=False))
            if 'refit_run_id' not in state:
                state['refit_run_id'] = 'learn-' + uuid.uuid4().hex
                _write(state_path, state)
            refit = self._refit(spec, selected, reports[selected['protocol_id']], prepared, working,
                                state['refit_run_id'], progress, check_cancel)
            registry = ModelRegistry(self.root).list_models()
            final_models = [row for row in registry['models'] if row['run_id'] == refit['run_id']]
            series = [row for row in registry['series'] if row['run_id'] == reports[selected['protocol_id']]['run_id']]
            if len(final_models) != 1 or len(series) != 1:
                raise ValueError('Selected study model/series publication is incomplete')
            if self._code_identity() != identity:
                raise ValueError('Learning implementation changed during the study')
            report = dict(family='ten_year_model_study_v1', study_id=study_id, run_id=study_id, status='completed',
                created_at=state['created_at'], completed_at=datetime.now().astimezone().isoformat(), spec=spec,
                read_ref=spec['read_ref'], universe=prepared['universe'], dataset=prepared['report'],
                candidates=summaries, seed_reviews=seed_reviews, seed_aggregates=reviewed, enhancements=enhancements,
                selected_protocol=selected, best_model_id=final_models[0]['model_id'], series_id=series[0]['series_id'],
                best_run_id=reports[selected['protocol_id']]['run_id'], refit_run_id=refit['run_id'],
                selection=dict(basis='development_fixed_seed_shortlist_then_predeclared_seed_aggregate',
                    ranking=[row['protocol_id'] for row in reviewed], initial_fixed_seed_ranking=[row['protocol_id'] for row in ranked],
                    key=['median_year_excess', 'stress_median_year_excess', 'sharpe', 'max_drawdown', 'worst_year_excess', 'turnover', 'rank_ic', 'simplicity'],
                    publication_seed=spec['seed'], negative_results_allowed=True,
                    protocol_selection_bias=True, historical_selector_is_oos=False, confirmation_used_for_selection=False),
                observed_periods=OBSERVED_PERIODS, qualification=self._qualification(), limitations=LIMITATIONS,
                implementation_sha256=identity, completed_runs=list(state['runs'].values()))
            _write(working / 'report.json', report)
            check_stop(check_cancel)
            (lock / 'owner.json').unlink()
            lock.rmdir()
            working.rename(self.runs_root / study_id)
            self._index(dict(study_id=study_id, run_id=study_id, status='completed', created_at=state['created_at'],
                spec=spec, best_model_id=report['best_model_id'], series_id=report['series_id'],
                report_sha256=digest(self.runs_root / study_id / 'report.json')))
            if progress:
                progress(dict(phase='completed', study_id=study_id, best_model_id=report['best_model_id']))
            return report
        except BaseException as error:
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding='utf-8'))
                state.update(status='cancelled' if error.__class__.__name__ == 'JobCancelled' else 'failed', error=str(error))
                _write(state_path, state)
                self._index({key: state[key] for key in ['study_id', 'created_at', 'spec', 'status', 'error']})
            raise
        finally:
            if lock.is_dir():
                (lock / 'owner.json').unlink(missing_ok=True)
                lock.rmdir()
