"""Final refit creates fresh inference identity without rewriting OOS evidence."""
import copy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from lake_equity.jobs import JobCancelled
from lake_equity.learning import dataset as dataset_module
from lake_equity.learning import refit as refit_module
from lake_equity.learning.registry import ModelRegistry, canonical
from lake_equity.learning.service import LearningService
from test_learning_service import synthetic_dataset, request


def make_study(root, monkeypatch, algorithm='ridge'):
    data = synthetic_dataset()
    monkeypatch.setattr(dataset_module, 'build_dataset', lambda *args, **kwargs: copy.deepcopy(data))
    spec = request(data)
    spec['models'] = [algorithm]
    if algorithm == 'mlp':
        spec['model_options'] = {'max_epochs': 3, 'patience': 2, 'device': 'cpu'}
    service = LearningService(root)
    parent = service.run(spec)
    return service, parent, data


@pytest.mark.parametrize('algorithm', ['ridge', 'mlp'])
def test_refit_preserves_oos_and_registers_only_a_fresh_frozen_model(tmp_path, monkeypatch, algorithm):
    service, parent, data = make_study(tmp_path, monkeypatch, algorithm)
    registry = ModelRegistry(tmp_path)
    before = registry.list_models()
    parent_root = service.runs_root / parent['run_id']
    parent_bytes = {str(path.relative_to(parent_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in parent_root.rglob('*') if path.is_file()}
    captured = {}
    actual_fit = refit_module.fit_predict

    def fitting(train, valid, inference, columns, **kwargs):
        captured.update(train=train.copy(), valid=valid.copy(), inference=inference.copy())
        result = actual_fit(train, valid, inference, columns, **kwargs)
        captured['scores'] = result[0]
        return result

    monkeypatch.setattr(refit_module, 'fit_predict', fitting)
    monkeypatch.setattr(refit_module, 'build_dataset', lambda *args, **kwargs: pytest.fail('Same-end refit must reuse verified frozen dataset'))
    report = refit_module.refit_model(tmp_path, {'run_id': parent['run_id'], 'model_name': 'factor_' + algorithm})
    assert report['purpose'] == 'final_refit'
    assert report['parent_run_id'] == parent['run_id']
    assert report['models'][0]['metrics'] == {} and report['models'][0]['portfolio'] == {}
    assert report['benchmark'] == {}
    assert 'predictions.parquet' not in report['artifacts']
    after = registry.list_models()
    registered = [model for model in after['models'] if model['run_id'] == report['run_id']]
    assert len(registered) == 1
    model = registered[0]
    assert model['model_id'] not in {row['model_id'] for row in before['models']}
    assert model['usage'] == 'frozen_inference_only'
    assert not [series for series in after['series'] if series['run_id'] == report['run_id']]
    assert {series['series_id'] for series in before['series']} == {series['series_id'] for series in after['series']}
    assert parent_bytes == {str(path.relative_to(parent_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in parent_root.rglob('*') if path.is_file()}
    assert service.read_run(parent['run_id']) == parent
    assert service.read_run(report['run_id']) == report
    train, valid = captured['train'], captured['valid']
    assert train.label_end.max() < valid.date.min()
    assert valid.label_end.max() < report['spec']['end']
    assert model['information_cutoff'] == max(train.label_end.max(), valid.label_end.max())
    assert captured['inference'].label.isna().all()
    scores, _ = registry.predict(model['model_id'], captured['inference'])
    np.testing.assert_allclose(scores, captured['scores'], rtol=2e-5, atol=1e-7)


def extended_dataset(data):
    result = copy.deepcopy(data)
    following = pd.bdate_range(data['calendar'][-1], periods=11).strftime('%Y-%m-%d').tolist()[1:]
    rows = []
    for offset, day in enumerate(following, 1):
        for _, source in data['frame'].groupby('symbol').tail(1).iterrows():
            row = source.to_dict()
            row.update(date=day, open=source.open + offset * .01, high=source.high + offset * .01,
                       low=source.low + offset * .01, close=source.close + offset * .01,
                       feature_eligible=True, legacy_ma_eligible=True)
            rows.append(row)
    result['calendar'] += following
    frame = pd.concat([result['frame'], pd.DataFrame(rows)], ignore_index=True).sort_values(['symbol', 'date'])
    for _, group in frame.groupby('symbol'):
        frame.loc[group.index, 'label'] = group.close.shift(-5) / group.open.shift(-1) - 1
        frame.loc[group.index, 'label_end'] = group.date.shift(-5)
    result['frame'] = frame.sort_values(['date', 'symbol']).reset_index(drop=True)
    result['sources']['read_ref'] = 'b' * 64
    result['report']['rows'] = len(frame)
    return result


def test_later_refit_rebuilds_run_context_hashes_and_dates(tmp_path, monkeypatch):
    service, parent, data = make_study(tmp_path, monkeypatch)
    later = extended_dataset(data)
    monkeypatch.setattr(refit_module, 'build_dataset', lambda *args, **kwargs: copy.deepcopy(later))
    report = refit_module.refit_model(tmp_path, {'run_id': parent['run_id'], 'model_name': 'factor_ridge',
                                              'end': later['calendar'][-1]})
    context = report['run_context']
    saved = service.runs_root / report['run_id']
    config = json.loads((saved / 'config.json').read_text())
    assert context['end_date'] == later['calendar'][-1]
    assert context['as_of_date'] == later['calendar'][-1]
    assert context['config_hash'] == hashlib.sha256(canonical(config).encode()).hexdigest()
    assert context['data_version'] == hashlib.sha256(canonical(later['sources']).encode()).hexdigest()
    assert context['data_version'] != parent['run_context']['data_version']
    assert report['sources']['read_ref'] == 'b' * 64
    assert context['task'] == 'final_refit'


def test_refit_rejects_recursive_protocol_and_past_end(tmp_path, monkeypatch):
    service, parent, _ = make_study(tmp_path, monkeypatch)
    original = {'run_id': parent['run_id'], 'model_name': 'factor_ridge'}
    with pytest.raises(ValueError, match='at or after'):
        refit_module.refit_model(tmp_path, dict(original, end=parent['spec']['start']))
    with pytest.raises(ValueError, match='explicitly'):
        refit_module.refit_model(tmp_path, dict(original, model_name='unknown'))
    result = refit_module.refit_model(tmp_path, original)
    with pytest.raises(ValueError, match='original evaluation protocol'):
        refit_module.refit_model(tmp_path, {'run_id': result['run_id'], 'model_name': 'factor_ridge'})


def test_refit_verifies_original_dataset_and_cancellation_does_not_register(tmp_path, monkeypatch):
    service, parent, _ = make_study(tmp_path, monkeypatch)
    before = ModelRegistry(tmp_path).list_models()
    cancelled = {'value': False}

    def progress(event):
        if event['phase'] == 'final_refit':
            cancelled['value'] = True

    request_value = {'run_id': parent['run_id'], 'model_name': 'factor_ridge'}
    with pytest.raises(JobCancelled):
        refit_module.refit_model(tmp_path, request_value, progress=progress, check_cancel=lambda: cancelled['value'])
    assert ModelRegistry(tmp_path).list_models() == before
    assert len(service.list_runs()['runs']) == 1
    original = service.runs_root / parent['run_id'] / 'dataset.parquet'
    original.write_bytes(original.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='dataset SHA256 mismatch'):
        refit_module.refit_model(tmp_path, request_value)
    assert ModelRegistry(tmp_path).list_models() == before
