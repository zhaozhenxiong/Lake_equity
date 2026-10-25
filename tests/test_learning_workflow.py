"""Frozen weights, chronological series, full denominators and API retirement."""
import copy
import json

import numpy as np
import pandas as pd
import polars as pl
import pytest

from lake_equity.learning import dataset as dataset_module
from lake_equity.learning import workflow as workflow_module
from lake_equity.learning.registry import ModelRegistry
from lake_equity.learning.releases import ReleaseRegistry
from lake_equity.learning.service import LearningService
from lake_equity.learning.workflow import LearningWorkflow
from lake_equity.market.store import MarketStore
from test_learning_service import synthetic_dataset, request
from test_learning_dataset import lake, publish


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    data = synthetic_dataset()
    monkeypatch.setattr(dataset_module, 'build_dataset', lambda *args, **kwargs: copy.deepcopy(data))
    spec = request(data)
    spec['models'] = ['ridge', 'lightgbm', 'mlp']
    spec['model_options'] = {'max_epochs': 3, 'patience': 2}
    report = LearningService(tmp_path).run(spec)
    return tmp_path, data, report, ModelRegistry(tmp_path)


@pytest.fixture(scope='module')
def workbench(tmp_path_factory):
    root = tmp_path_factory.mktemp('workbench')
    store = MarketStore(root)
    days = pd.bdate_range('2024-01-02', periods=300).strftime('%Y-%m-%d').tolist()
    publish(store, 'trading_calendar', pl.DataFrame({'trade_date': pd.to_datetime(days).date.tolist(), 'is_trading': True}))
    symbols = ['600000.SH', '600001.SH', '600002.SH', '600003.SH', '000001.SZ', '000002.SZ']
    parts = []
    for number, symbol in enumerate(symbols):
        price = 10. + number * .5 + np.arange(len(days)) * .01
        volume = 100_000_000. if number != 3 else 1_000.
        parts.append(pd.DataFrame(dict(symbol=symbol, trade_date=pd.to_datetime(days).date, open=price,
            high=price + .1, low=price - .1, close=price + .02, volume=volume,
            amount=price * volume, source='sina_unadjusted_v1')))
    publish(store, 'daily_bars', pl.from_pandas(pd.concat(parts, ignore_index=True)))
    LearningService(root).run(dict(symbols=symbols, start=days[0], end=days[-1], factor_groups=['price_volume'],
        horizon=5, train_sessions=40, valid_sessions=10, test_sessions=10, max_folds=2, models=['ridge'],
        top_k=2, initial_cash=100_000.))
    registry = ModelRegistry(root)
    series = next(row for row in registry.list_models()['series'] if row['model_name'] == 'factor_ridge')
    policy = dict(version='liquidity_coverage_v1', history_sessions=252, recent_sessions=60, min_coverage=.95,
                  min_recent_coverage=.95, min_median_amount=1_000_000., max_members=3)
    releases = ReleaseRegistry(root)
    release = releases.create(dict(model_id=series['members'][0]['model_id'], series_id=series['series_id'],
        name='测试默认方法', universe=dict(policy=policy)))
    releases.activate(release['release_id'], reason='Synthetic backend-core test release')
    plain = next(row for row in registry.list_models()['models'] if row['model_name'] == 'legacy_ma_ridge')
    plain_release = releases.create(dict(model_id=plain['model_id'], name='无序列默认方法'))
    return dict(root=root, days=days, symbols=symbols, series=series, release=release,
                plain_release=plain_release, policy=policy)



def test_registered_weights_reproduce_saved_oos_without_fit(frozen, monkeypatch):
    root, data, report, registry = frozen
    import sklearn.linear_model
    monkeypatch.setattr(sklearn.linear_model.Ridge, 'fit', lambda *args, **kwargs: pytest.fail('inference fitted a model'))
    saved = pd.read_parquet(root / 'var/learning/runs' / report['run_id'] / 'predictions.parquet')
    listed = registry.list_models()
    assert len(listed['models']) == 10  # MA Ridge/MLP + 3 factor models, two folds
    for model in listed['models']:
        subset = saved.loc[saved.model_name.eq(model['model_name']) & saved.fold.eq(model['fold'])]
        frame = data['frame'].merge(subset[['date', 'symbol']], on=['date', 'symbol'])
        result, checked = registry.predict(model['model_id'], frame)
        assert checked['information_cutoff'] < subset.date.min()
        expected = frame[['date', 'symbol']].merge(subset[['date', 'symbol', 'score']], on=['date', 'symbol']).score
        np.testing.assert_allclose(result, expected, rtol=2e-5, atol=1e-7)
    assert len(listed['series']) == 5
    assert registry.register_run(report['run_id'])['models']
    assert len(registry.list_models()['models']) == 10


def test_corrupted_frozen_weights_fail_closed(frozen):
    root, data, report, registry = frozen
    model = next(row for row in registry.list_models()['models'] if row['model_type'] == 'mlp')
    (registry.directory / model['model_id'] / 'weights.npz').write_bytes(b'changed')
    with pytest.raises(ValueError, match='SHA256'):
        registry.predict(model['model_id'], data['frame'])


def test_rolling_backtest_reloads_folds_and_rejects_in_sample_period(frozen):
    root, data, report, registry = frozen
    series = next(row for row in registry.list_models()['series'] if row['model_name'] == 'factor_mlp')
    params = dict(mode='rolling', series_id=series['series_id'], start=series['members'][0]['start'],
                  end=series['members'][-1]['end'], top_k=2, initial_cash=100_000.)
    workflow = LearningWorkflow(root)
    result = workflow.backtest(params)
    assert result['model_ids'] == [row['model_id'] for row in series['members']]
    assert result['portfolio']['trades']
    assert workflow.read_run(result['run_id']) == result
    assert workflow.list_runs()['reports'][0]['run_id'] == result['run_id']
    with pytest.raises(ValueError, match='out-of-sample'):
        workflow.backtest(dict(params, start=data['calendar'][0]))
    with pytest.raises(ValueError, match='after all training'):
        workflow.backtest(dict(mode='frozen', model_id=result['model_ids'][0],
                              start=data['calendar'][0], end=params['end']))


def test_frozen_backtest_rejects_empty_symbol_scope_before_creating_run(frozen):
    root, data, report, registry = frozen
    model = next(row for row in registry.list_models()['models'] if row['model_name'] == 'factor_ridge')
    directory = root / 'var/learning/workflows'
    building = lambda: sorted(directory.glob('.*.building')) if directory.is_dir() else []
    before = building()
    params = dict(mode='frozen', model_id=model['model_id'], start=data['calendar'][-1],
                  end=data['calendar'][-1], scope='symbols')
    workflow = LearningWorkflow(root)
    for invalid in (params, dict(params, symbols=[])):
        with pytest.raises(ValueError, match='non-empty symbol list'):
            workflow.backtest(invalid)
    with pytest.raises(ValueError, match='Unknown model backtest scope'):
        workflow.backtest(dict(params, scope='watchlist', symbols=model['population']))
    assert building() == before


def test_read_only_registry_and_workflow_do_not_create_directories(tmp_path):
    root = tmp_path / 'unused'
    assert ModelRegistry(root).list_models() == {'models': [], 'series': []}
    assert LearningWorkflow(root).list_runs() == {'reports': []}
    assert not root.exists()


def test_uncovered_symbol_batch_keeps_population_as_missing(lake):
    root, store, days, bars = lake
    session = dataset_module.DatasetSession(root)
    data = dataset_module.build_dataset(root, dict(symbols=['000002.SZ'], start=str(days[180]), end=str(days[-1])), session=session)
    session.verify()
    assert len(data['frame']) == 60
    assert data['frame'].symbol.eq('000002.SZ').all()
    assert not data['frame'].bar_valid.any()
    assert not data['frame'].feature_eligible.any()
    assert data['report']['symbols_with_bars'] == 0


def test_required_empty_factor_group_is_blocked(tmp_path, monkeypatch):
    data = synthetic_dataset()
    monkeypatch.setattr(dataset_module, 'build_dataset', lambda *args, **kwargs: copy.deepcopy(data))
    spec = request(data)
    spec['factor_groups'] = ['price_volume', 'margin']
    with pytest.raises(ValueError, match='Required factor group'):
        LearningService(tmp_path).run(spec)
    assert LearningService(tmp_path).list_runs() == {'runs': []}


def test_scan_batches_share_one_model_and_rank_complete_population(frozen, monkeypatch):
    root, data, report, registry = frozen
    model = next(row for row in registry.list_models()['models'] if row['model_name'] == 'factor_mlp')
    symbols = [f'{index:06d}.SZ' for index in range(401)]
    latest = data['calendar'][-1]
    bars = root / 'latest.parquet'
    pd.DataFrame({'trade_date': [date_value for date_value in pd.to_datetime([latest]).date]}).to_parquet(bars)
    sessions = []

    class Session:
        def __init__(self, *args):
            self.store = None
            self.snapshot = {'read_ref': 'b' * 64}
            self.evidence = {}
            self.check_cancel = args[2] if len(args) > 2 else None
            self.verified = False
            sessions.append(self)

        def source_files(self, *args):
            return {'files': [{'path': str(bars)}]}

        def verify(self):
            self.verified = True

    batches = []

    def build(root, spec, **kwargs):
        assert spec['start'] == report['spec']['start']
        batches.append(spec['symbols'])
        rows = []
        base = data['frame'].iloc[-1].to_dict()
        for i, symbol in enumerate(spec['symbols']):
            row = dict(base, date=latest, symbol=symbol, bar_valid=True, feature_eligible=symbol != symbols[0],
                       legacy_ma_eligible=True, price_a=float(int(symbol[:6])) / 400.)
            rows.append(row)
        return {'frame': pd.DataFrame(rows)}

    monkeypatch.setattr(workflow_module, 'DatasetSession', Session)
    monkeypatch.setattr(workflow_module, 'build_dataset', build)
    monkeypatch.setattr(LearningWorkflow, '_sessions', lambda *args: data['calendar'])
    loads = []
    original = workflow_module.load_saved

    def load(*args, **kwargs):
        loads.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow_module, 'load_saved', load)
    result = LearningWorkflow(root).scan(dict(model_id=model['model_id'], scope='symbols', symbols=symbols, end=latest))
    assert [len(batch) for batch in batches] == [200, 200, 1]
    assert len(loads) == 1
    assert result['population'] == dict(total=401, scored=400, missing=1, failed=0, unprocessed=0,
                                       sha256=result['population']['sha256'])
    assert len(result['rows']) == 401
    assert sorted(row['rank'] for row in result['rows'] if row['status'] == 'scored') == list(range(1, 401))
    assert all(row['is_training_member'] == (row['symbol'] in model['population']) for row in result['rows'])
    assert sessions[0].verified
    assert result['model_version'] == model['version']
    assert result['feature_history_start'] == report['spec']['start']


def test_release_universe_scope_keeps_five_to_three_behavior(workbench):
    root, days, release, policy, symbols = (workbench['root'], workbench['days'], workbench['release'],
                                            workbench['policy'], workbench['symbols'])
    cutoff = ModelRegistry(root).read_model(release['model_id'])['information_cutoff']
    assert days[290] > cutoff
    workflow = LearningWorkflow(root)
    params = dict(mode='frozen', release_id='active', expected_release_id=release['release_id'],
                  scope='symbols', symbols=symbols, start=days[290], end=days[299])
    result = workflow.backtest(params)
    assert result['release_id'] == release['release_id']
    assert result['requested_release_id'] == 'active'
    effective = result['cohort_effective']
    assert effective['requested'] == 6 and effective['scored'] == 3
    assert effective['universe_scope'] == 'release' and effective['cap_applied'] is True
    assert effective['release_universe_policy'] == policy
    assert {row['symbol'] for row in effective['excluded']} == {'600000.SH', '600001.SH', '600003.SH'}
    assert sorted(row['reason'] for row in effective['excluded']) == [
        'liquidity_below_threshold', 'rank_beyond_cap', 'rank_beyond_cap']
    assert all(isinstance(row['rank'], int) and 1 <= row['rank'] <= 5
               for row in effective['excluded'] if row['reason'] == 'rank_beyond_cap')
    assert effective['unavailable'] == []
    assert result['population']['total'] == 6
    with pytest.raises(ValueError, match='after all training'):
        workflow.backtest(dict(params, start=days[0]))


def test_explicit_symbols_applies_qualification_without_cap(workbench):
    root, days, release, policy, symbols = (workbench['root'], workbench['days'], workbench['release'],
                                            workbench['policy'], workbench['symbols'])
    result = LearningWorkflow(root).backtest(dict(mode='frozen', release_id='active', scope='symbols',
        symbols=symbols, start=days[290], end=days[299], universe_scope='explicit_symbols'))
    effective = result['cohort_effective']
    assert effective['requested'] == 6 and effective['scored'] == 5
    assert effective['universe_scope'] == 'explicit_symbols' and effective['cap_applied'] is False
    assert effective['release_universe_policy'] == policy
    assert [(row['symbol'], row['reason']) for row in effective['excluded']] == [
        ('600003.SH', 'liquidity_below_threshold')]
    assert effective['excluded'][0]['amount60_median'] < policy['min_median_amount']
    assert effective['unavailable'] == []
    scores = pd.read_parquet(root / 'var/learning/workflows' / result['run_id'] / 'scores.parquet')
    assert set(scores.symbol) == set(symbols) - {'600003.SH'}


def test_watchlist_replay_scores_saved_folds_without_market_widening(workbench):
    root, days, release, symbols = workbench['root'], workbench['days'], workbench['release'], workbench['symbols']
    requested = ['600000.SH', '600002.SH', '000001.SZ', '600001.SH', '600004.SH']
    result = LearningWorkflow(root).backtest(dict(mode='watchlist_replay', release_id=release['release_id'],
        scope='symbols', symbols=requested, start=days[280], end=days[299]))
    assert result['mode'] == 'watchlist_replay' and result['series_id'] == release['series_id']
    assert result['restricted_cross_section'] is True
    assert result['watchlist_selection_bias'] == 'post_hoc_user_watchlist'
    assert result['original_cohort_size'] == len(symbols)
    assert result['restricted_cohort_size'] == 4
    assert result['weights_chronology'] == 'saved_oos_members'
    assert result['fit_oos'] is False and result['oos_ranking_eligible'] is False
    assert result['actual_start'] == days[280] and result['actual_end'] == days[299]
    assert result['population']['symbols'] == requested
    effective = result['cohort_effective']
    assert effective['requested'] == 5 and effective['scored'] == 4
    assert effective['universe_scope'] == 'release' and effective['cap_applied'] is False
    assert effective['excluded'] == []
    assert effective['unavailable'] == [{'symbol': '600004.SH', 'reason': 'not_in_saved_evaluation_data'}]
    assert any('评价人口被用户事后限制' in limitation for limitation in result['limitations'])
    scores = pd.read_parquet(root / 'var/learning/workflows' / result['run_id'] / 'scores.parquet')
    assert set(scores.symbol) == set(requested) - {'600004.SH'}
    assert set(scores.model_name) == {release['series_id']}
    # The API layer pre-fills the release model identity; that identity must be
    # accepted for watchlist replay instead of tripping the rolling-series guard.
    forwarded = LearningWorkflow(root).backtest(dict(mode='watchlist_replay', release_id=release['release_id'],
        model_id=release['model_id'], scope='symbols', symbols=requested, start=days[280], end=days[299]))
    assert forwarded['restricted_cohort_size'] == 4


def test_watchlist_replay_and_expected_release_fail_closed(workbench):
    root, days, release = workbench['root'], workbench['days'], workbench['release']
    workflow = LearningWorkflow(root)
    base = dict(mode='watchlist_replay', release_id=release['release_id'], scope='symbols',
                symbols=['600000.SH', '000001.SZ'], start=days[280], end=days[299])
    with pytest.raises(ValueError, match='watchlist_replay requires scope=symbols'):
        workflow.backtest({key: value for key, value in base.items() if key != 'scope'})
    with pytest.raises(ValueError, match='out-of-sample series'):
        workflow.backtest(dict(base, start=days[100], end=days[150]))
    with pytest.raises(ValueError, match='No saved evaluation data'):
        workflow.backtest(dict(base, start=days[280], end=days[289], symbols=['600004.SH']))
    with pytest.raises(ValueError, match='saved historical series'):
        workflow.backtest(dict(base, release_id=workbench['plain_release']['release_id']))
    with pytest.raises(ValueError, match='universe_scope must be release or explicit_symbols'):
        workflow.backtest(dict(mode='frozen', release_id='active', scope='symbols', symbols=['600000.SH'],
            start=days[290], end=days[299], universe_scope='magic'))
    with pytest.raises(ValueError, match='explicit_symbols universe_scope requires'):
        workflow.backtest(dict(mode='rolling', release_id='active', scope='symbols', symbols=['600000.SH'],
            start=days[290], end=days[299], universe_scope='explicit_symbols'))
    with pytest.raises(ValueError, match='默认方法已变更，请刷新后重试'):
        workflow.backtest(dict(mode='frozen', release_id='active', expected_release_id='release-' + 'f' * 32,
            scope='symbols', symbols=['600000.SH'], start=days[290], end=days[299]))
    with pytest.raises(ValueError, match='默认方法已变更，请刷新后重试'):
        workflow.scan(dict(release_id='active', expected_release_id='release-' + 'f' * 32,
            scope='symbols', symbols=['600000.SH'], end=days[299]))
    replay = workflow.backtest(dict(mode='research_replay', release_id='active', scope='symbols',
        symbols=['000002.SZ'], start=days[290], end=days[299], universe_scope='explicit_symbols'))
    assert replay['mode'] == 'research_replay'
    assert replay['cohort_effective']['universe_scope'] == 'release'  # accepted but ignored


def test_bplan_plan_reports_are_saved_and_readable(workbench):
    root, days, model_id = workbench['root'], workbench['days'], workbench['release']['model_id']
    workflow = LearningWorkflow(root)
    identity, working = workflow._start('bplan')
    report = dict(family='backtest_plan_v1', kind='bplan', run_id=identity, status='completed',
        created_at='2026-10-24T15:30:00+08:00', read_ref='a' * 64, as_of=days[299], model_id=model_id,
        model_ids=[model_id], spec=dict(signal_date=days[299], execution_date=days[299]), _symbols=['000002.SZ'],
        plan=dict(schema='backtest_conditional_plan_v1'))
    saved = workflow._save(report, working)
    assert saved['family'] == 'backtest_plan_v1' and saved['kind'] == 'bplan'
    assert workflow.read_run(identity)['run_id'] == identity
    assert [row['run_id'] for row in workflow.list_runs(kind='bplan')['reports']] == [identity]


def test_backtest_preview_reads_per_symbol_eligibility(workbench):
    from lake_equity.learning.preview import BacktestPreview

    root, days, symbols = workbench['root'], workbench['days'], workbench['symbols']
    response = BacktestPreview(root).preview(dict(mode='frozen', release_id='active', scope='symbols',
        symbols=symbols, start=days[290], end=days[299], universe_scope='explicit_symbols'))
    assert response['status'] == 'ready'
    assert response['universe_scope'] == 'explicit_symbols' and response['cap_applied'] is False
    assert response['counts'] == dict(requested=6, eligible=5, excluded=1)
    rows = {row['symbol']: row for row in response['per_symbol']}
    assert rows['600003.SH']['status'] == 'excluded'
    assert rows['600003.SH']['reason'] == 'liquidity_below_threshold'
    assert rows['600000.SH']['status'] == 'eligible' and rows['600000.SH']['rank'] is None
    assert rows['600000.SH']['coverage252'] == pytest.approx(1.)


def test_scan_universe_scope_and_cohort_effective(workbench):
    root, days, release, policy, symbols = (workbench['root'], workbench['days'], workbench['release'],
                                            workbench['policy'], workbench['symbols'])
    workflow = LearningWorkflow(root)
    released = workflow.scan(dict(release_id='active', scope='symbols', symbols=symbols, end=days[299], top_k=3))
    assert released['family'] == 'model_scan_v1' and released['requested_release_id'] == 'active'
    effective = released['cohort_effective']
    assert effective['requested'] == 6 and effective['scored'] == 3
    assert effective['universe_scope'] == 'release' and effective['cap_applied'] is True
    assert effective['release_universe_policy'] == policy
    assert released['population']['excluded'] == 3
    excluded = {row['symbol']: row for row in released['rows'] if row['status'] == 'excluded'}
    assert excluded['600003.SH']['reason'] == 'liquidity_below_threshold'
    assert excluded['600000.SH']['reason'] == 'rank_beyond_cap'
    explicit = workflow.scan(dict(release_id='active', scope='symbols', symbols=symbols, end=days[299],
        universe_scope='explicit_symbols', top_k=3))
    effective = explicit['cohort_effective']
    assert effective['requested'] == 6 and effective['scored'] == 5
    assert effective['universe_scope'] == 'explicit_symbols' and effective['cap_applied'] is False
    assert [(row['symbol'], row['reason']) for row in effective['excluded']] == [
        ('600003.SH', 'liquidity_below_threshold')]
    assert explicit['population']['excluded'] == 1
    rows = {row['symbol']: row for row in explicit['rows']}
    assert rows['600003.SH']['status'] == 'excluded'
    assert rows['600003.SH']['reason'] == 'liquidity_below_threshold'
    assert all(rows[symbol]['status'] == 'scored' for symbol in symbols if symbol != '600003.SH')


def test_watchlist_replay_out_of_series_dates_fail_before_creating_run(workbench):
    root, release, series = workbench['root'], workbench['release'], workbench['series']
    first = min(row['start'] for row in series['members'])
    last = max(row['end'] for row in series['members'])
    beyond = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    before_series = (pd.Timestamp(first) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    workflow = LearningWorkflow(root)
    directory = root / 'var/learning/workflows'
    before = sorted(directory.glob('.*.building')) if directory.is_dir() else []
    with pytest.raises(ValueError, match='Watchlist replay dates must lie within the saved out-of-sample series') as error:
        workflow.backtest(dict(mode='watchlist_replay', release_id=release['release_id'], scope='symbols',
                               symbols=workbench['symbols'][:2], start=first, end=beyond,
                               top_k=2, initial_cash=100_000.))
    assert f'({first}..{last})' in str(error.value) and beyond in str(error.value)
    with pytest.raises(ValueError, match='Watchlist replay dates must lie within the saved out-of-sample series'):
        workflow.backtest(dict(mode='watchlist_replay', release_id=release['release_id'], scope='symbols',
                               symbols=workbench['symbols'][:2], start=before_series, end=last,
                               top_k=2, initial_cash=100_000.))
    after = sorted(directory.glob('.*.building')) if directory.is_dir() else []
    assert after == before, 'rejected watchlist replay must not leave a build directory'

