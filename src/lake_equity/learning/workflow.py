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
from .models import check_stop, explain_saved, load_saved, rolling_folds
from .population import population
from .registry import ModelRegistry, canonical, digest


def _day(value, name):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError(name + ' must be an ISO date')
    return value


def _number(value):
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _explicit_symbols(value):
    if not isinstance(value, list) or not value or len(value) > 6000 or len(set(value)) != len(value) or any(
            not isinstance(symbol, str) or not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', symbol) for symbol in value):
        raise ValueError('Provide unique explicit Shanghai/Shenzhen A-share symbols, or a published all-market population')
    return value


def _universe_scope(params, mode, default_scope=None):
    value = params.get('universe_scope', 'release')
    if value not in {'release', 'explicit_symbols'}:
        raise ValueError('universe_scope must be release or explicit_symbols')
    if value == 'explicit_symbols':
        if params.get('scope', default_scope) != 'symbols' or mode not in {'scan', 'frozen', 'research_replay', 'watchlist_replay'}:
            raise ValueError('explicit_symbols universe_scope requires scope=symbols with an explicit non-empty symbol pool in a frozen or replay mode')
        _explicit_symbols(params.get('symbols'))
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
            rows = [json.loads(row[0]) for row in conn.execute('SELECT summary FROM runs ORDER BY rowid DESC LIMIT ?', (-1 if limit is None else limit,))]
        return {'reports': [row for row in rows if kind is None or row['kind'] == kind]}

    def read_run(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'(?:scan|backtest|bplan)-[0-9a-f]{32}', run_id):
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
        summary.update(id=report['run_id'], title={'scan': '模型选股', 'backtest': '模型回测', 'bplan': '次日研究计划'}.get(
                           report['kind'], report['kind']),
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
        params = dict(params)
        allowed = {'model_id', 'release_id', 'scope', 'symbols', 'end', 'read_ref', 'top_k',
                   'universe_scope', 'expected_release_id'}
        if set(params) - allowed:
            raise ValueError('Unknown model scan parameters: ' + ', '.join(sorted(set(params) - allowed)))
        if params.get('scope') == 'all' and params.get('symbols'):
            raise ValueError('Do not combine all-market scope with explicit symbols')
        top_k = params.get('top_k', 20)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError('top_k must be an integer in [1,100]')
        date_resolution = dict(source='explicit_request', warning=None)
        if params.get('end') is None:
            from lake_equity.pipeline import _default_target_date
            target, _, source, error = _default_target_date(self.root)
            if target is None:
                raise ValueError('Cannot resolve the last closed trading session: ' + str(error or 'calendar unavailable'))
            params['end'] = target
            date_resolution = dict(source=source, warning=error)
        end = _day(params.get('end'), 'end')
        requested_release_id = params.get('release_id')
        universe_scope = _universe_scope(params, 'scan', default_scope='symbols')
        from .releases import ReleaseRegistry
        release = ReleaseRegistry(self.root).resolve(params) if params.get('release_id') or not params.get('model_id') else None
        if release:
            params.update(release_id=release['release_id'], model_id=release['model_id'])
        if params.get('expected_release_id') and (release is None or release['release_id'] != params['expected_release_id']):
            raise ValueError('默认方法已变更，请刷新后重试')
        model = self.registry.read_model(params.get('model_id'))
        infer = load_saved(self.registry.directory_for(model['model_id']))
        history_start = self._history_start(model)
        session = DatasetSession(self.root, params.get('read_ref'), check_cancel)
        sources = {}
        calendar = self._sessions(session, '9999-12-31', sources)
        requested_calendar = [day for day in calendar if day <= end]
        if not requested_calendar:
            raise ValueError('No verified trading session at the requested cutoff')
        # The requested cutoff may be a holiday or a session whose bars have not
        # been published. Resolve the actual market date, and disclose both.
        evidence = session.source_files('daily_bars', '1900-01-01', end)
        if not evidence['files']:
            raise ValueError('No published daily bars')
        latest = (pl.scan_parquet([row['path'] for row in evidence['files']], hive_partitioning=False)
                  .filter(pl.col('trade_date') <= date.fromisoformat(end)).select(pl.col('trade_date').max()).collect().item())
        if latest is None:
            raise ValueError('No published bars at the requested cutoff')
        as_of = min(requested_calendar[-1], str(latest)[:10])
        if as_of <= model['information_cutoff']:
            raise ValueError('Frozen inference date must follow all training and validation information')
        cohort = population(session, params.get('symbols'), scope=params.get('scope', 'symbols'), end=as_of)
        run_id, working = self._start('scan')
        universe = None
        eligibility = None
        policy = (release or {}).get('universe', {}).get('policy')
        if universe_scope == 'explicit_symbols':
            from .universe import eligibility_for_symbols
            eligibility = eligibility_for_symbols(session, cohort['symbols'], as_of, as_of,
                policy=policy, check_cancel=check_cancel)
            universe_symbols = set(eligibility.loc[eligibility.eligible.astype(bool), 'symbol'])
        else:
            universe_symbols = set(cohort['symbols'])
            if release and policy:
                from .universe import build_universe, universe_on_date
                universe = build_universe(self.root, as_of, as_of, self.root / 'var/learning/universes' / run_id,
                    policy=release['universe']['policy'], session=session, progress=progress, check_cancel=check_cancel)
                universe_symbols = set(universe_on_date(universe, as_of)['symbols'])
        rows = {symbol: dict(symbol=symbol, score=None, rank=None, status='unprocessed', reason='not_processed',
                             is_training_member=symbol in model['population']) for symbol in cohort['symbols']}
        if session.snapshot.get('datasets', {}).get('instruments', {}).get('available'):
            catalog = _read(session.store, session.snapshot, 'instruments', '1900-01-01', as_of,
                            cohort['symbols'], sources, check_cancel, session)
            if {'symbol', 'name'} <= set(catalog):
                for symbol, group in catalog.groupby('symbol'):
                    names = {value.strip() for value in group['name'] if isinstance(value, str) and value.strip()}
                    if symbol in rows and len(names) == 1:
                        rows[symbol]['name'] = next(iter(names))
        if eligibility is not None:
            reasons = {row.symbol: str(row.reason) for row in eligibility.itertuples()}
            for symbol in set(rows) - universe_symbols:
                rows[symbol].update(status='excluded', reason=reasons.get(symbol, 'no_valid_bar_as_of'))
        elif universe:
            for symbol in set(rows) - universe_symbols:
                rows[symbol].update(status='excluded', reason='outside_as_of_coverage_liquidity_universe')
        inference_symbols = [symbol for symbol in cohort['symbols'] if symbol in universe_symbols]
        explanation_rows, quotes = {}, {}
        cancelled = False
        from lake_equity.jobs import JobCancelled
        try:
            for offset in range(0, len(inference_symbols), 200):
                check_stop(check_cancel)
                symbols = inference_symbols[offset:offset + 200]
                data = build_dataset(self.root, dict(symbols=symbols, start=history_start, end=as_of,
                    horizon=model['horizon'], factor_groups=model['factor_groups']), session=session,
                    check_cancel=check_cancel)
                frame = data['frame'].loc[data['frame'].date.eq(as_of)]
                eligible = frame.feature_eligible.eq(True) & frame.legacy_ma_eligible.eq(True)
                candidate = frame.loc[eligible]
                scores = infer(candidate) if len(candidate) else []
                explained = explain_saved(self.registry.directory_for(model['model_id']), candidate,
                    scores=scores, feature_groups=data.get('feature_groups'))
                explanation_rows.update({row['symbol']: row for row in explained['rows']})
                for observation, score in zip(candidate.to_dict('records'), scores):
                    row = rows[observation['symbol']]
                    row.update(score=float(score), status='scored', reason=None,
                               imputed_feature_count=int(sum(not np.isfinite(float(observation[col])) for col in model['columns'])))
                    if observation['symbol'] in explanation_rows:
                        row.update(explanation_summary=explanation_rows[observation['symbol']]['summary'],
                                   explanation_ref=dict(run_id=run_id, symbol=observation['symbol']))
                from .scan_explanations import quote_context
                quotes.update(quote_context(data['frame'], as_of, symbols))
                for observation in frame.loc[~eligible].to_dict('records'):
                    rows[observation['symbol']].update(status='missing', reason='no_valid_bar_as_of' if not observation['bar_valid'] else 'insufficient_verified_history')
                if progress:
                    progress(dict(phase='model_scan', current=min(offset + 200, len(inference_symbols)), total=len(inference_symbols),
                                  population_total=cohort['total'],
                                  model_id=model['model_id'], as_of=as_of))
        except JobCancelled:
            cancelled = True
            session.check_cancel = None
        session.verify()
        counts = {status: sum(row['status'] == status for row in rows.values()) for status in ['scored', 'missing', 'failed', 'unprocessed']}
        if universe or eligibility is not None:
            counts['excluded'] = sum(row['status'] == 'excluded' for row in rows.values())
        complete = counts['unprocessed'] == 0 and counts['failed'] == 0 and not cancelled
        ordered = sorted((row for row in rows.values() if row['status'] == 'scored'), key=lambda row: (-row['score'], row['symbol']))
        if complete:
            for rank, row in enumerate(ordered, 1):
                row['rank'] = rank
        if release:
            for row in rows.values():
                row['release_id'] = release['release_id']
        scored_symbols = {row['symbol'] for row in rows.values() if row['status'] == 'scored'}
        missing = [symbol for symbol in cohort['symbols'] if symbol not in scored_symbols]
        excluded = []
        if eligibility is not None:
            amounts = {row.symbol: _number(row.amount60_median) for row in eligibility.itertuples()}
            excluded = [dict(symbol=symbol, reason=rows[symbol]['reason'], amount60_median=amounts.get(symbol))
                        for symbol in cohort['symbols'] if rows[symbol]['status'] == 'excluded']
            unavailable = [dict(symbol=symbol, reason=rows[symbol].get('reason') or 'no_valid_bar_as_of')
                           for symbol in missing if rows[symbol]['status'] != 'excluded']
            cap_applied = False
        elif universe:
            from .universe import metrics_for_symbols
            excluded_symbols = [symbol for symbol in cohort['symbols'] if rows[symbol]['status'] == 'excluded']
            metrics = metrics_for_symbols(universe, as_of, as_of, excluded_symbols) if excluded_symbols else None
            for symbol in excluded_symbols:
                rows_for = metrics.loc[metrics.symbol.eq(symbol)] if metrics is not None else None
                record = rows_for.iloc[-1] if rows_for is not None and len(rows_for) else None
                if record is None:
                    reason, rank, amount = 'outside_as_of_coverage_liquidity_universe', None, None
                elif not bool(record['eligible']):
                    reason, rank, amount = str(record['reason']), None, _number(record['amount60_median'])
                else:
                    reason, rank, amount = 'rank_beyond_cap', int(record['rank']), _number(record['amount60_median'])
                rows[symbol]['reason'] = reason
                excluded.append(dict(symbol=symbol, reason=reason, rank=rank, amount60_median=amount))
            unavailable = [dict(symbol=symbol, reason=rows[symbol].get('reason') or 'no_valid_bar_as_of')
                           for symbol in missing if rows[symbol]['status'] != 'excluded']
            cap_applied = True
        else:
            unavailable = [dict(symbol=symbol, reason=rows[symbol].get('reason') or 'no_valid_bar_as_of') for symbol in missing]
            cap_applied = False
        cohort_effective = dict(requested=cohort['total'], scored=len(scored_symbols), universe_scope=universe_scope,
            cap_applied=cap_applied, release_universe_policy=policy, excluded=excluded, unavailable=unavailable)
        (working / 'population.json').write_text(canonical(cohort), encoding='utf-8')
        pd.DataFrame(rows.values()).to_parquet(working / 'scores.parquet', index=False)
        (working / 'quotes.json').write_text(canonical(quotes), encoding='utf-8')
        report = dict(family='model_scan_v1', kind='scan', run_id=run_id, lake_run_id=run_id,
            created_at=datetime.now().astimezone().isoformat(), status='cancelled' if cancelled else 'completed' if complete else 'partial',
            spec=params, model_id=model['model_id'], model_version=model['version'], information_cutoff=model['information_cutoff'],
            frozen_at=model['frozen_at'], as_of=as_of, requested_cutoff=end, end=as_of, scope=cohort['scope'],
            date_resolution=date_resolution,
            population=dict(total=cohort['total'], **counts, sha256=cohort['sha256']),
            cohort_effective=cohort_effective,
            source_context=dict(read_ref=session.snapshot['read_ref'], as_of=as_of,
                next_session=next((day for day in calendar if day > as_of), None),
                calendar_recent=[day for day in calendar if day <= as_of][-64:],
                reviewed_symbols=[symbol for symbol, row in rows.items() if row['status'] != 'unprocessed'],
                quotes=dict(artifact='quotes.json', sha256=digest(working / 'quotes.json'), count=len(quotes),
                            reviewed_symbols=list(quotes))),
            rows=ordered + [row for row in rows.values() if row['status'] != 'scored'],
            top=ordered[:top_k] if complete else [], final_ranking=complete,
            feature_history_start=history_start,
            requested_release_id=requested_release_id,
            release_id=release['release_id'] if release else None, release_identity=release, universe=universe,
            read_ref=session.snapshot['read_ref'], sources={str(key): value for key, value in session.evidence.items()},
            _symbols=cohort['symbols'],
            qualification=model['qualification'],
            purpose='historical_reconstruction' if as_of < model['frozen_at'][:10] else 'frozen_inference',
            limitations=['Current published population is a frozen research cohort; it is not certified historical membership.',
                          'Missing factors use the original training medians; per-row imputation counts are disclosed.',
                          'Close-derived ranks are research for the following verified session, not executable orders.'])
        from .scan_explanations import save_explanations
        report['explanation'] = save_explanations(working, report, model, explanation_rows)
        return self._save(report, working)

    def backtest(self, params, progress=None, check_cancel=None):
        from .portfolio import backtest_scores
        from .service import LearningService
        params = dict(params)
        allowed = {'mode', 'model_id', 'series_id', 'release_id', 'study_id', 'start', 'end', 'scope', 'symbols', 'read_ref',
                   'top_k', 'initial_cash', 'rebalance_sessions', 'cost_stress', 'require_verified_rules',
                   'max_total_weight', 'max_stock_weight', 'max_volume_participation',
                   'hold_buffer', 'drawdown_guard', 'drawdown_scale', 'vol_target', 'vol_lookback',
                   'reconstruction_requested_start', 'reconstruction_requested_end',
                   'historical_protocol_selection_bias', 'parent_study_id',
                   'universe_scope', 'expected_release_id'}
        if set(params) - allowed:
            raise ValueError('Unknown model backtest parameters: ' + ', '.join(sorted(set(params) - allowed)))
        start, end = _day(params.get('start'), 'start'), _day(params.get('end'), 'end')
        if start > end:
            raise ValueError('start must not exceed end')
        mode = params.get('mode', 'frozen')
        if mode not in {'frozen', 'rolling', 'historical', 'research_replay', 'watchlist_replay'}:
            raise ValueError('Unknown model backtest mode')
        if mode in {'frozen', 'research_replay'}:
            scope = params.get('scope')
            if scope is not None and scope not in ('all', 'symbols'):
                raise ValueError('Unknown model backtest scope: ' + repr(scope))
            if scope == 'symbols' and not params.get('symbols'):
                raise ValueError('Explicit symbol scope requires a non-empty symbol list of Shanghai/Shenzhen A-share symbols; omit scope to use the frozen model original training population')
        requested_release_id = params.get('release_id')
        universe_scope = _universe_scope(params, mode)
        if mode == 'watchlist_replay':
            if params.get('scope') != 'symbols' or not params.get('symbols'):
                raise ValueError('watchlist_replay requires scope=symbols with an explicit non-empty symbol list')
            _explicit_symbols(params.get('symbols'))
        if universe_scope == 'explicit_symbols' and mode == 'research_replay':
            # The long-window historical replay keeps the default release cohort
            # semantics; the shared frontend contract still sends the flag.
            universe_scope = 'release'
        from .releases import ReleaseRegistry
        release = ReleaseRegistry(self.root).resolve(params) if params.get('release_id') or not any(params.get(k) for k in ['model_id', 'series_id', 'study_id']) else None
        if release is None:
            # The default method must keep one contract no matter which entry
            # mode the page used. A rolling series or model identity that equals
            # the active release inherits that release's risk and universe.
            active = ReleaseRegistry(self.root).active()
            if active:
                if params.get('series_id') and params['series_id'] == active.get('series_id'):
                    release = active
                elif params.get('model_id') and params['model_id'] == active.get('model_id'):
                    release = active
        if release:
            params['release_id'] = release['release_id']
            for key in ['top_k', 'initial_cash', 'rebalance_sessions', 'max_total_weight', 'max_stock_weight']:
                params.setdefault(key, release['risk'][key])
            if mode in {'rolling', 'watchlist_replay'}:
                params.setdefault('series_id', release['series_id'])
            elif mode != 'historical':
                params.setdefault('model_id', release['model_id'])
        if params.get('expected_release_id') and (release is None or release['release_id'] != params['expected_release_id']):
            raise ValueError('默认方法已变更，请刷新后重试')
        if mode == 'watchlist_replay' and (release is None or not release.get('series_id')):
            raise ValueError('Watchlist replay requires the research release to expose a saved historical series')
        if mode == 'watchlist_replay':
            if params.get('model_id') and params['model_id'] != release['model_id']:
                raise ValueError('Requested model differs from the frozen strategy release')
            if params.get('series_id') and params['series_id'] != release['series_id']:
                raise ValueError('Requested series_id differs from the frozen strategy release')
            members = (self.registry.read_series(release['series_id']).get('members') or [])
            if not members:
                raise ValueError('Saved rolling series has no members')
            first, last = min(row['start'] for row in members), max(row['end'] for row in members)
            if start < first or end > last:
                raise ValueError(f'Watchlist replay dates must lie within the saved out-of-sample series ({first}..{last}); requested {start}..{end}')
        if mode == 'historical':
            from .study import StudyService
            if params.get('scope') or params.get('symbols'):
                raise ValueError('Historical protocol reconstruction uses its frozen dynamic universe; scope and symbols overrides are not allowed')
            request = {key: value for key, value in params.items() if key not in {'mode', 'model_id', 'series_id', 'release_id'}}
            if release:
                request['study_id'] = release['study_id']
            parent = StudyService(self.root).read_run(request.get('study_id'))
            if parent.get('status') != 'completed':
                raise ValueError('Historical reconstruction requires a completed research protocol')
            saved = self.registry.read_series(parent['series_id'])
            earliest = min(row['start'] for row in saved['members'])
            latest = max(row['end'] for row in saved['members'])
            if earliest <= start <= end <= latest and (not params.get('read_ref') or params['read_ref'] == parent['read_ref']):
                replay = {key: value for key, value in params.items() if key not in {'mode', 'model_id', 'series_id', 'study_id', 'scope', 'symbols', 'read_ref'}}
                replay.update(mode='rolling', series_id=saved['series_id'], parent_study_id=parent['study_id'],
                    reconstruction_requested_start=start, reconstruction_requested_end=end,
                    historical_protocol_selection_bias=True)
                return self.backtest(replay, progress=progress, check_cancel=check_cancel)
            return StudyService(self.root).historical_backtest(request, progress=progress, check_cancel=check_cancel)
        if mode != 'watchlist_replay' and params.get('model_id') and params.get('series_id'):
            raise ValueError('Do not combine a fixed model and a rolling series')
        if params.get('scope') == 'all' and params.get('symbols'):
            raise ValueError('Do not combine all-market scope with explicit symbols')
        run_id, working = self._start('backtest')
        model_ids = []
        universe = None
        replay_fields = {}
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
            dataset_query = pl.scan_parquet(directory / 'dataset.parquet')
            saved_days = sorted(dataset_query.select('date').unique().collect()['date'].to_list())
            decision_calendar = saved_days
            if start < saved_days[0] or end > saved_days[-1]:
                period = DatasetSession(self.root, report['sources']['read_ref'], check_cancel)
                decision_calendar = self._sessions(period, end, {})
                period.verify()
            requested_sessions = [day for day in decision_calendar if start <= day <= end]
            if not requested_sessions:
                raise ValueError('Requested interval contains no verified trading sessions')
            start, end = requested_sessions[0], requested_sessions[-1]
            earliest = min(row['start'] for row in series['members'])
            latest = max(row['end'] for row in series['members'])
            if start < earliest or end > latest:
                raise ValueError(f'Rolling dates must lie within the saved out-of-sample series ({earliest}..{latest}); requested {start}..{end}')
            preceding = [day for day in saved_days if day < start]
            prefix = preceding[-1] if preceding else start
            data = dataset_query.filter(pl.col('date').is_between(pl.lit(prefix), pl.lit(end))).collect().to_pandas()
            scores = []
            for member in series['members']:
                check_stop(check_cancel)
                model = self.registry.read_model(member['model_id'])
                selected = data.loc[data.date.between(max(start, member['start']), min(end, member['end'])) &
                                    data.feature_eligible.eq(True) & data.legacy_ma_eligible.eq(True)].copy()
                if 'universe_eligible' in selected:
                    selected = selected.loc[selected.universe_eligible.eq(True)].copy()
                if selected.empty:
                    continue
                selected['score'] = load_saved(self.registry.directory_for(model['model_id']))(selected)
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
            universe = report.get('universe')
            evidence = dict(evaluation_run_id=report['run_id'], sources=report['sources'],
                            evaluation_dataset_sha256=report['artifacts']['dataset.parquet']['sha256'])
            scored_symbols = set(predictions.symbol)
            cohort_effective = dict(requested=cohort['total'], scored=len(scored_symbols),
                universe_scope='release', cap_applied=False,
                release_universe_policy=release.get('universe', {}).get('policy') if release else None,
                excluded=[], unavailable=[dict(symbol=symbol, reason='no_valid_bar_as_of')
                                          for symbol in cohort['symbols'] if symbol not in scored_symbols])
        elif mode == 'watchlist_replay':
            series = self.registry.read_series(release['series_id'])
            service = LearningService(self.root)
            saved_report = service.read_run(series['run_id'])
            directory = service.runs_root / series['run_id']
            for name in ['dataset.parquet', 'predictions.parquet']:
                if digest(directory / name) != saved_report['artifacts'][name]['sha256']:
                    raise ValueError('Frozen evaluation dataset SHA256 mismatch')
            requested = _explicit_symbols(params.get('symbols'))
            members = series['members']
            if start < min(row['start'] for row in members) or end > max(row['end'] for row in members):
                raise ValueError('Watchlist replay dates must lie within the saved out-of-sample series')
            saved_dataset = pl.scan_parquet(directory / 'dataset.parquet')
            saved_days = sorted(saved_dataset.select('date').unique().collect()['date'].to_list())
            requested_sessions = [day for day in saved_days if start <= day <= end]
            if not requested_sessions:
                raise ValueError('Requested interval contains no saved verified trading sessions')
            start, end = requested_sessions[0], requested_sessions[-1]
            preceding = [day for day in saved_days if day < start]
            prefix = preceding[-1] if preceding else start
            data = (saved_dataset.filter(pl.col('date').is_between(pl.lit(prefix), pl.lit(end)))
                    .filter(pl.col('symbol').is_in(requested)).collect().to_pandas())
            saved_symbols = set(data.symbol)
            if 'universe_eligible' in data:
                data = data.loc[data.universe_eligible.fillna(False).astype(bool)].copy()
            if data.empty:
                raise ValueError('No saved evaluation data for the requested watchlist interval')
            scores = []
            for member in members:
                check_stop(check_cancel)
                model = self.registry.read_model(member['model_id'])
                selected = data.loc[data.date.between(max(start, member['start']), min(end, member['end'])) &
                                    data.feature_eligible.eq(True) & data.legacy_ma_eligible.eq(True)].copy()
                if selected.empty:
                    continue
                selected['score'] = load_saved(self.registry.directory_for(model['model_id']))(selected)
                selected['fold'], selected['train_end'] = member['fold'], model['information_cutoff']
                selected['model_id'] = model['model_id']
                scores.append(selected)
                model_ids.append(model['model_id'])
            if not scores:
                raise ValueError('No eligible saved out-of-sample observations for the requested watchlist')
            predictions = pd.concat(scores, ignore_index=True)
            calendar = sorted(set(data.date))
            prices = data.loc[data.date <= end]
            cohort = dict(symbols=requested, total=len(requested), policy='explicit_watchlist_replay_cohort')
            scored_symbols = set(predictions.symbol)
            unavailable = [dict(symbol=symbol, reason='not_in_saved_evaluation_data' if symbol not in saved_symbols
                                else 'no_valid_bar_as_of') for symbol in requested if symbol not in scored_symbols]
            cohort_effective = dict(requested=len(requested), scored=len(scored_symbols),
                universe_scope=universe_scope, cap_applied=False,
                release_universe_policy=(release.get('universe') or {}).get('policy'),
                excluded=[], unavailable=unavailable)
            snapshot, qualification = saved_report['sources']['read_ref'], saved_report['qualification']
            evidence = dict(evaluation_run_id=saved_report['run_id'], sources=saved_report['sources'],
                            evaluation_dataset_sha256=saved_report['artifacts']['dataset.parquet']['sha256'])
            replay_fields = dict(restricted_cross_section=True, watchlist_selection_bias='post_hoc_user_watchlist',
                original_cohort_size=len(saved_report['spec'].get('symbols') or []),
                restricted_cohort_size=len(scored_symbols), weights_chronology='saved_oos_members')
        else:
            model = self.registry.read_model(params.get('model_id'))
            if start <= model['information_cutoff'] and mode != 'research_replay':
                raise ValueError('Fixed-model backtest must begin after all training and validation information')
            infer = load_saved(self.registry.directory_for(model['model_id']))
            history_start = self._history_start(model)
            if mode == 'research_replay':
                history_start = min(history_start, start)
            model_ids.append(model['model_id'])
            session = DatasetSession(self.root, params.get('read_ref'), check_cancel)
            calendar = self._sessions(session, end, {})
            evidence_files = session.source_files('daily_bars', '1900-01-01', end)['files']
            latest = (pl.scan_parquet([row['path'] for row in evidence_files], hive_partitioning=False)
                      .filter(pl.col('trade_date') <= date.fromisoformat(end))
                      .select(pl.col('trade_date').max()).collect().item()) if evidence_files else None
            requested_sessions = [day for day in calendar if start <= day <= end]
            if not requested_sessions or latest is None:
                raise ValueError('No published verified trading sessions in the requested interval')
            if requested_sessions[-1] > str(latest)[:10]:
                raise ValueError('Requested interval exceeds published daily bars; latest available date is ' + str(latest)[:10])
            start, end = requested_sessions[0], requested_sessions[-1]
            calendar = [day for day in calendar if day <= end]
            before = [day for day in calendar if day < start]
            prefix = before[-1] if before else start
            cohort = population(session, None if params.get('scope') == 'all' else params.get('symbols') or model['population'],
                                scope=params.get('scope', 'symbols'), end=end)
            policy = (release or {}).get('universe', {}).get('policy')
            eligibility = None
            if universe_scope == 'explicit_symbols':
                from .universe import eligibility_for_symbols
                eligibility = eligibility_for_symbols(session, cohort['symbols'], start, end,
                    policy=policy, check_cancel=check_cancel)
            elif release and policy:
                from .universe import build_universe
                universe = build_universe(self.root, start, end, self.root / 'var/learning/universes' / run_id,
                    policy=release['universe']['policy'], session=session, progress=progress, check_cancel=check_cancel)
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
                if universe:
                    from .universe import apply_membership
                    selected = apply_membership(selected, universe)
                if eligibility is not None:
                    selected = selected.merge(eligibility.loc[eligibility.eligible.astype(bool), ['date', 'symbol']],
                                              on=['date', 'symbol'], how='inner')
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
            scored_symbols = set(predictions.symbol)
            missing = [symbol for symbol in cohort['symbols'] if symbol not in scored_symbols]
            excluded = []
            if eligibility is not None:
                for symbol in missing:
                    rows = eligibility.loc[eligibility.symbol.eq(symbol)]
                    if rows.empty:
                        excluded.append(dict(symbol=symbol, reason='no_valid_bar_as_of'))
                    elif not rows.eligible.astype(bool).any():
                        last = rows.iloc[-1]
                        excluded.append(dict(symbol=symbol, reason=str(last['reason']),
                                             amount60_median=_number(last['amount60_median'])))
                excluded_symbols = {row['symbol'] for row in excluded}
                unavailable = [dict(symbol=symbol, reason='no_valid_bar_as_of')
                               for symbol in missing if symbol not in excluded_symbols]
                cap_applied = False
            elif universe is not None:
                from .universe import load_membership, metrics_for_symbols
                members = set(load_membership(universe, start, end).symbol)
                excluded_symbols = [symbol for symbol in missing if symbol not in members]
                metrics = metrics_for_symbols(universe, end, end, excluded_symbols) if excluded_symbols else None
                for symbol in excluded_symbols:
                    rows_for = metrics.loc[metrics.symbol.eq(symbol)] if metrics is not None else None
                    record = rows_for.iloc[-1] if rows_for is not None and len(rows_for) else None
                    if record is None:
                        excluded.append(dict(symbol=symbol, reason='no_valid_bar_as_of'))
                    elif not bool(record['eligible']):
                        excluded.append(dict(symbol=symbol, reason=str(record['reason']),
                                             amount60_median=_number(record['amount60_median'])))
                    else:
                        excluded.append(dict(symbol=symbol, reason='rank_beyond_cap', rank=int(record['rank']),
                                             amount60_median=_number(record['amount60_median'])))
                unavailable = [dict(symbol=symbol, reason='no_valid_bar_as_of')
                               for symbol in missing if symbol in members]
                cap_applied = True
            else:
                unavailable = [dict(symbol=symbol, reason='no_valid_bar_as_of') for symbol in missing]
                cap_applied = False
            cohort_effective = dict(requested=cohort['total'], scored=len(scored_symbols), universe_scope=universe_scope,
                cap_applied=cap_applied, release_universe_policy=policy, excluded=excluded, unavailable=unavailable)
        predictions['model_name'] = params.get('series_id') if mode in {'rolling', 'watchlist_replay'} else model_ids[0]
        predictions['available_at'] = predictions.date + 'T15:00:00+08:00'
        calendar = [day for day in calendar if day <= end]
        predictions[['date', 'symbol', 'score', 'model_name', 'fold', 'train_end', 'model_id', 'available_at']].to_parquet(working / 'scores.parquet', index=False)
        from .execution import PRICE_COLUMNS, EXECUTION_EVIDENCE_COLUMNS
        prices.loc[prices.date.le(end), [column for column in PRICE_COLUMNS + EXECUTION_EVIDENCE_COLUMNS if column in prices]].to_parquet(working / 'prices.parquet', index=False)
        config = {key: params[key] for key in ['top_k', 'initial_cash', 'rebalance_sessions', 'cost_stress', 'require_verified_rules',
                                                'hold_buffer', 'drawdown_guard', 'drawdown_scale', 'vol_target', 'vol_lookback'] if key in params}
        config.update({key: params[key] for key in ['max_total_weight', 'max_stock_weight', 'max_volume_participation'] if key in params})
        portfolio = backtest_scores(prices, predictions, calendar, config, run_id=run_id, check_cancel=check_cancel,
                                    research_replay=mode == 'research_replay')
        (working / 'population.json').write_text(canonical(cohort), encoding='utf-8')
        report = dict(family='model_backtest_v1', kind='backtest', run_id=run_id, lake_run_id=run_id,
            created_at=datetime.now().astimezone().isoformat(), status='completed', spec=params, mode=mode,
            model_id=params.get('model_id'), series_id=params.get('series_id'), model_ids=model_ids,
            portfolio=portfolio, population=cohort, cohort_effective=cohort_effective, read_ref=snapshot, qualification=qualification,
            model_versions={identity: self.registry.read_model(identity)['version'] for identity in model_ids},
            sources=evidence, _symbols=cohort['symbols'],
            requested_release_id=requested_release_id,
            release_id=release['release_id'] if release else None, release_identity=release,
            universe=universe,
            actual_start=start, actual_end=end,
            fit_oos=mode not in {'research_replay', 'watchlist_replay'},
            oos_ranking_eligible=mode not in {'research_replay', 'watchlist_replay'},
            protocol_selection_scope='retrospective_frozen_protocol',
            historical_reconstruction={key: params[key] for key in ['reconstruction_requested_start',
                'reconstruction_requested_end', 'historical_protocol_selection_bias', 'parent_study_id'] if key in params},
            purpose='historical_reconstruction',
            limitations=['The experiment was frozen at the recorded wall-clock time; reconstruction does not prove historical live availability.',
                          'Current or explicit cohorts are not certified point-in-time historical membership. No automatic paper allocation.'])
        report.update(replay_fields)
        if mode == 'watchlist_replay':
            report['limitations'] = report['limitations'] + [
                '评价人口被用户事后限制，不是原全市场样本外成绩。',
                'Restricted cross-section: only the explicitly requested watchlist symbols were scored with saved fold weights; no market-wide ranking exists.']
        return self._save(report, working)
