"""Chronology, immutable publication and resumability of the study workflow."""
import copy
import json

import numpy as np
import pandas as pd
import pytest

from lake_equity.jobs import JobCancelled
from lake_equity.learning.registry import ModelRegistry, digest
from lake_equity.learning.service import LearningService
from lake_equity.learning.study import StudyService, selection_key, study_folds, validate_spec


def panel():
    days = pd.bdate_range('2024-01-02', periods=130).strftime('%Y-%m-%d').tolist()
    rows = []
    for number, symbol in enumerate(['600000.SH', '000001.SZ', '600519.SH']):
        opening = 10.+number+np.arange(len(days))*.003
        closing = opening+.02*np.sin(np.arange(len(days))/4+number)
        for i, day in enumerate(days):
            mature = i+5 < len(days)
            rows.append(dict(date=day, symbol=symbol, open=opening[i], close=closing[i], high=opening[i]+.1,
                low=opening[i]-.1, volume=10000000., amount=opening[i]*10000000., bar_valid=True,
                label=closing[i+5]/opening[i+1]-1 if mature else np.nan,
                label_end=days[i+5] if mature else None, feature_eligible=True, legacy_ma_eligible=True,
                universe_eligible=not (number == 0 and i >= 100), a=np.sin(i/8+number), b=np.cos(i/7+number)))
    return days, pd.DataFrame(rows)


def request(days, **extra):
    return dict(start=days[0], end=days[-1], train_windows=[40, 50], valid_sessions=10, test_sessions=10,
        holdout_sessions=20, models=['ridge'], seeds=[42], seed_review_count=1, backend='local',
        include_margin=False, **extra)


def install(monkeypatch, service, days, frame):
    monkeypatch.setattr(service, '_calendar', lambda spec: (days, 'a'*64))
    def prepare(spec, working, progress, check_cancel):
        path = working/'batch.parquet'
        frame.to_parquet(path, index=False)
        frame.to_parquet(working/'dataset.parquet', index=False)
        return dict(partitions=[dict(path=path.name, sha256=digest(path), rows=len(frame))],
            dataset_artifact=dict(path='dataset.parquet', sha256=digest(working/'dataset.parquet')),
            symbols=sorted(frame.symbol.unique()), calendar=days, feature_groups=dict(price_volume=['a', 'b'], legacy_ma=['a']),
            report=dict(rows=len(frame), label_formula='synthetic', limitations=['Synthetic test only']),
            sources=dict(read_ref='a'*64, datasets={}), universe=dict(universe_id='test-universe'))
    monkeypatch.setattr(service, '_prepare', prepare)


def test_all_common_folds_and_exact_confirmation_boundary():
    days = pd.bdate_range('2010-01-01', periods=2428).strftime('%Y-%m-%d').tolist()
    spec = validate_spec(dict(start=days[0], end=days[-1]))
    short, long = (study_folds(days, spec, length) for length in spec['train_windows'])
    assert len(short) > 12
    assert [(v['test_start'], v['test_end']) for v in short] == [(v['test_start'], v['test_end']) for v in long]
    dev = [v for v in long if v['segment'] == 'development']
    conf = [v for v in long if v['segment'] == 'confirmation']
    assert dev[-1]['test_end'] < conf[0]['test_start']
    assert conf[0]['test_start'] == days[-252]
    covered = [day for fold in long for day in days if fold['test_start'] <= day <= fold['test_end']]
    assert len(covered) == len(set(covered))


def test_portfolio_date_aggregations_do_not_repeat_per_calendar_session(tmp_path, monkeypatch):
    days, frame = panel()
    frame.to_parquet(tmp_path/'dataset.parquet', index=False)
    scores = frame.loc[frame.date.isin(days[60:80]), ['date', 'symbol']].copy()
    scores['score'], scores['model_name'], scores['fold'] = 1., 'test', 0
    scores['train_end'], scores['available_at'] = days[59], scores.date+'T15:00:00+08:00'
    calls = {'min': 0, 'max': 0}
    for name in calls:
        original = getattr(pd.Series, name)
        def counted(series, *args, _name=name, _original=original, **kwargs):
            if series.name == 'date':
                calls[_name] += 1
            return _original(series, *args, **kwargs)
        monkeypatch.setattr(pd.Series, name, counted)
    service = StudyService(tmp_path)
    result = service._portfolio(tmp_path, {}, scores, days, validate_spec(request(days)), 'bounded-date-reductions', None)
    assert result['evaluation_start'] == days[60]
    assert result['evaluation_end'] == days[79]
    assert result['trades']
    assert calls == {'min': 1, 'max': 1}


def test_selection_ignores_confirmation_and_accepts_negative_results():
    summary = dict(protocol_id='a', complexity=0,
        development=dict(median_year_excess=-.1, stress_median_year_excess=-.12, max_drawdown=.2,
                         worst_year_excess=-.3, turnover=2., rank_ic=-.01), confirmation={'total_return': -999})
    changed = copy.deepcopy(summary)
    changed['confirmation'] = {'total_return': 999}
    assert selection_key(summary) == selection_key(changed)
    better = copy.deepcopy(summary)
    better['development']['median_year_excess'] = -.05
    assert selection_key(better) < selection_key(summary)


def test_real_ridge_study_freezes_dynamic_predictions_and_latest_refit(tmp_path, monkeypatch):
    days, frame = panel()
    service = StudyService(tmp_path)
    install(monkeypatch, service, days, frame)
    report = service.run(request(days))
    assert service.read_run(report['study_id']) == report
    assert service.list_runs()['studies'][0]['best_model_id'] == report['best_model_id']
    assert report['selection']['confirmation_used_for_selection'] is False
    assert report['selection']['publication_seed'] == 42
    assert len(report['candidates']) == 4
    registry = ModelRegistry(tmp_path)
    model = registry.read_model(report['best_model_id'])
    series = registry.read_series(report['series_id'])
    assert model['usage'] == 'frozen_inference_only'
    assert model['information_cutoff'] < days[-1]
    refit = LearningService(tmp_path).read_run(report['refit_run_id'])
    assert refit['models'][0]['metrics'] == {}
    for candidate in report['candidates']:
        saved = LearningService(tmp_path).read_run(candidate['run_id'])
        scores = pd.read_parquet(tmp_path/'var/learning/runs'/candidate['run_id']/'predictions.parquet')
        assert not ((scores.date >= days[100]) & scores.symbol.eq('600000.SH')).any()
        assert (scores.train_end < scores.date).all()
        for fold in saved['folds']:
            assert fold['fit_end'] < fold['test_start']
            assert fold['purged_train_rows'] > 0
        assert saved['models'][0]['portfolio']['config']['max_total_weight'] == .8
        development = saved['evaluations']['development']['portfolio']
        dev_folds = [row for row in saved['folds'] if row['segment'] == 'development']
        assert max(row['date'] for row in development['daily']) == dev_folds[-1]['test_end']
        assert max(row['date'] for row in development['daily']) < saved['evaluations']['confirmation']['portfolio']['daily'][0]['date']
    assert len(series['members']) == 7


def test_cancel_after_fold_and_resume_reuses_fitted_weights(tmp_path, monkeypatch):
    days, frame = panel()
    service = StudyService(tmp_path)
    install(monkeypatch, service, days, frame)
    cancelled = {'yes': False}
    def progress(value):
        if value['phase'] == 'study_fold':
            cancelled['yes'] = True
    with pytest.raises(JobCancelled):
        service.run(request(days), progress=progress, check_cancel=lambda: cancelled['yes'])
    summary = service.list_runs()['studies'][0]
    assert summary['status'] == 'cancelled'
    cached = list((tmp_path/'var/learning/runs').glob('.learn-*.building/fold-0.json'))
    assert len(cached) == 1
    original = json.loads(cached[0].read_text())
    weights = next((cached[0].parent/'models').rglob('model.json'))
    before = (digest(weights), weights.stat().st_mtime_ns)
    report = service.run(dict(resume_study_id=summary['study_id']))
    assert report['status'] == 'completed'
    completed = weights.parents[3].name.removeprefix('.').removesuffix('.building')
    published = tmp_path/'var/learning/runs'/completed/weights.relative_to(weights.parents[3])
    assert (digest(published), published.stat().st_mtime_ns) == before
    assert original['fold']['fit_end'] < original['fold']['test_start']


def test_reads_do_not_create_storage_and_invalid_request_fails_first(tmp_path):
    service = StudyService(tmp_path/'unused')
    assert service.list_runs() == {'studies': []}
    with pytest.raises(KeyError):
        service.read_run('study-'+'a'*32)
    with pytest.raises(ValueError):
        service.run(dict(start='2024-01-01', end='2023-01-01'))
    assert not service.root.exists()


def test_resume_reuses_completed_universe_and_first_feature_batch(tmp_path, monkeypatch):
    from lake_equity.learning import study as module
    from lake_equity.learning import universe
    days, frame = panel()
    frame = frame.loc[frame.symbol.eq('600000.SH')].drop(columns='universe_eligible')
    spec = validate_spec(request(days))
    spec['read_ref'] = 'a'*64
    calls = {'universe': 0, 'dataset': 0}
    artifact = dict(read_ref='a'*64, policy=spec['universe'], start=days[0], end=days[-1],
                    symbols=['600000.SH'], universe_id='fixture')
    class Session:
        def __init__(self, *args):
            self.snapshot = dict(read_ref='a'*64)
        def verify(self):
            pass
    def build(*args, **kwargs):
        calls['universe'] += 1
        directory = args[3]
        directory.mkdir(parents=True)
        (directory/'manifest.json').write_text('{}')
        return artifact
    def dataset(*args, **kwargs):
        calls['dataset'] += 1
        return dict(frame=frame.copy(), calendar=days, feature_groups=dict(price_volume=['a', 'b'], legacy_ma=['a']),
                    report=dict(rows=len(frame)), sources=dict(read_ref='a'*64))
    monkeypatch.setattr(module, 'DatasetSession', Session)
    monkeypatch.setattr(module, 'build_dataset', dataset)
    monkeypatch.setattr(universe, 'build_universe', build)
    monkeypatch.setattr(universe, 'read_universe', lambda value: artifact)
    monkeypatch.setattr(universe, 'load_membership', lambda value: frame[['date', 'symbol']])
    working = tmp_path/'.study-cache.building'
    working.mkdir()
    service = StudyService(tmp_path)
    def cancel(event):
        if event['phase'] == 'study_features':
            raise JobCancelled('test interruption after the first committed feature batch')
    with pytest.raises(JobCancelled):
        service._prepare(spec, working, cancel, None)
    result = service._prepare(spec, working, None, None)
    assert calls == {'universe': 1, 'dataset': 1}
    assert result['report']['rows'] == len(frame)
    assert (working/'dataset.parquet').is_file()


@pytest.mark.parametrize('missing_second_factor', [False, True])
def test_two_feature_batches_align_column_names_and_preserve_missing(tmp_path, monkeypatch, missing_second_factor):
    from lake_equity.learning import study as module
    from lake_equity.learning import universe
    import pyarrow.parquet as pq
    days, _ = panel()
    symbols = [f'600{index:03d}.SH' for index in range(201)]
    spec = validate_spec(request(days))
    spec['include_margin'] = True
    spec['read_ref'] = 'a'*64
    artifact = dict(read_ref='a'*64, policy=spec['universe'], start=days[0], end=days[-1],
                    symbols=symbols, universe_id='two-batches')
    membership = pd.DataFrame(dict(date=[days[0]]*len(symbols), symbol=symbols))
    class Session:
        def __init__(self, *args):
            self.snapshot = dict(read_ref='a'*64)
        def verify(self):
            pass
    def dataset(root, batch_spec, **kwargs):
        batch = batch_spec['symbols']
        frame = pd.DataFrame(dict(date=[days[0]]*len(batch), symbol=batch,
            a=[1.]*len(batch), margin_a=[2.]*len(batch), margin_b=[3.]*len(batch)))
        if batch[0] == symbols[-1]:
            if missing_second_factor:
                frame = frame.drop(columns='margin_b')
            frame = frame[frame.columns[::-1]]
        n = len(batch)
        first = n > 1
        coverage = dict(columns=['margin_a', 'margin_b'], nonempty_rows=n,
            complete_rows=0 if not first and missing_second_factor else n,
            nonempty_values=n*2-(1 if not first and missing_second_factor else 0), row_coverage=1., reason='observed_nonempty')
        return dict(frame=frame, calendar=days, feature_groups=dict(price_volume=['a'], legacy_ma=['a'], margin=['margin_a', 'margin_b']),
            report=dict(rows=n, symbols_with_bars=n, valid_bar_rows=n-1 if first else n,
                feature_eligible_rows=n-2 if first else n, label_reason_counts=dict(ok=n-1 if first else n, immature=1 if first else 0),
                group_coverage=dict(margin=coverage)),
            sources=dict(read_ref='a'*64, datasets=dict(daily_bars=dict(revision_id='frozen',
                files=[dict(path='source-'+batch[0], sha256='a'*64)]))))
    monkeypatch.setattr(module, 'DatasetSession', Session)
    monkeypatch.setattr(module, 'build_dataset', dataset)
    monkeypatch.setattr(universe, 'build_universe', lambda *args, **kwargs: artifact)
    monkeypatch.setattr(universe, 'load_membership', lambda value: membership)
    working = tmp_path/'.study-two-batches.building'
    working.mkdir()
    service = StudyService(tmp_path)
    prepared = service._prepare(spec, working, None, None)
    frame = service._frame(working, prepared, days[0], days[-1])
    assert len(frame) == 201
    assert frame.a.eq(1.).all() and frame.margin_a.eq(2.).all()
    last = frame.loc[frame.symbol.eq(symbols[-1])].iloc[0]
    if missing_second_factor:
        assert pd.isna(last.margin_b)
    else:
        assert last.margin_b == 3.
    assert frame.loc[frame.symbol.ne(symbols[-1]), 'margin_b'].eq(3.).all()
    assert pq.read_schema(working/'dataset.parquet').names == prepared['dataset_columns']
    report = prepared['report']
    assert report['symbols_with_bars'] == 201 and report['valid_bar_rows'] == 200 and report['feature_eligible_rows'] == 199
    assert report['label_reason_counts'] == dict(ok=200, immature=1)
    assert report['group_coverage']['margin']['nonempty_rows'] == 201
    assert report['group_coverage']['margin']['nonempty_values'] == (401 if missing_second_factor else 402)
    assert report['group_coverage']['margin']['row_coverage'] == 1.
    assert len(prepared['sources']['datasets']['daily_bars']['files']) == 2
    assert service._prepare(spec, working, None, None)['report'] == report
    document = json.loads((working/'prepared.json').read_text())
    document['report']['valid_bar_rows'] = 999
    (working/'prepared.json').write_text(json.dumps(document))
    with pytest.raises(ValueError, match='aggregate coverage'):
        service._prepare(spec, working, None, None)


def test_historical_reconstruction_rejects_silent_universe_override(tmp_path):
    with pytest.raises(ValueError, match='scope/symbol overrides'):
        StudyService(tmp_path).historical_backtest(dict(study_id='study-'+'a'*32, scope='symbols', symbols=['600000.SH']))
