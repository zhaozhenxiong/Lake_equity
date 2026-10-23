"""Frozen weights, chronological series, full denominators and API retirement."""
import copy
import json

import numpy as np
import pandas as pd
import pytest

from lake_equity.learning import dataset as dataset_module
from lake_equity.learning import workflow as workflow_module
from lake_equity.learning.registry import ModelRegistry
from lake_equity.learning.service import LearningService
from lake_equity.learning.workflow import LearningWorkflow
from test_learning_service import synthetic_dataset, request
from test_learning_dataset import lake


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    data = synthetic_dataset()
    monkeypatch.setattr(dataset_module, 'build_dataset', lambda *args, **kwargs: copy.deepcopy(data))
    spec = request(data)
    spec['models'] = ['ridge', 'lightgbm', 'mlp']
    spec['model_options'] = {'max_epochs': 3, 'patience': 2}
    report = LearningService(tmp_path).run(spec)
    return tmp_path, data, report, ModelRegistry(tmp_path)


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
