"""Synthetic MLP regression and frozen inference; no production data access."""
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from lake_equity.jobs import JobCancelled
from lake_equity.learning import models, neural
from lake_equity.learning.models import fit_predict, predict_saved, validate_model_options


def sample():
    rng = np.random.default_rng(104)
    columns = [f'feature_{index}' for index in range(43)] + ['empty_in_train']
    values = rng.normal(size=(180, 43))
    frame = pd.DataFrame(values, columns=columns[:-1])
    frame['empty_in_train'] = np.nan
    frame['date'] = pd.bdate_range('2025-01-02', periods=len(frame)).strftime('%Y-%m-%d')
    frame['symbol'] = '600000.SH'
    frame['label'] = .015 * values[:, 0] - .01 * values[:, 1] + .005 * values[:, 2] ** 2
    frame.loc[170:, 'label'] = np.nan
    frame.loc[7, 'feature_3'] = np.nan
    return frame.iloc[:120].copy(), frame.iloc[120:160].copy(), frame.iloc[160:].copy(), columns


def fit(directory, *, model_type='mlp', backend='local', options=None):
    train, valid, test, columns = sample()
    config = dict(device='cpu', max_epochs=8, patience=3, batch_size=32)
    config.update(options or {})
    result = fit_predict(train, valid, test, columns, model_type=model_type, backend=backend,
                         seed=42, directory=directory, root=directory.parent,
                         model_options=config if model_type == 'mlp' else None)
    return result, test, columns


def test_mlp_consumes_dynamic_multifactor_inputs_and_reload_matches(tmp_path):
    import torch
    previous_threads = torch.get_num_threads()
    (scores, details), test, _ = fit(tmp_path / 'model')
    assert details['architecture']['input_dimensions'] == 43
    assert details['architecture']['widths'] == [64, 32]
    assert details['feature_count'] == 43
    assert details['preprocessing']['dropped_training_empty'] == ['empty_in_train']
    assert details['actual_device'] == 'cpu'
    assert details['seed'] == 42
    assert details['weights_format'] == 'numpy_npz_no_pickle'
    assert details['best_epoch'] == min(details['learning_curve'], key=lambda row: row['valid_mse'])['epoch']
    assert details['epochs_completed'] <= 8
    assert torch.get_num_threads() == previous_threads
    assert np.isfinite(scores).all() and len(scores) == 20
    assert test.label.tail(10).isna().all()
    loaded = predict_saved(tmp_path / 'model', test.drop(columns=['date', 'symbol', 'label']))
    np.testing.assert_allclose(loaded, scores, rtol=1e-6, atol=1e-7)
    with np.load(tmp_path / 'model/weights.npz', allow_pickle=False) as arrays:
        assert all(arrays[key].dtype == np.float32 for key in arrays.files)


@pytest.mark.parametrize('model_type', ['ridge', 'lightgbm'])
def test_existing_model_types_also_support_frozen_reload(tmp_path, model_type):
    (scores, details), test, _ = fit(tmp_path / model_type, model_type=model_type)
    loaded = predict_saved(tmp_path / model_type, test)
    np.testing.assert_allclose(loaded, scores, rtol=1e-12, atol=1e-12)
    assert details['model_type'] == model_type


@pytest.mark.parametrize('explicit_names', [False, True])
def test_qlib_positional_native_names_preserve_semantic_feature_order(tmp_path, explicit_names):
    directory = tmp_path / 'model'
    (scores, details), test, _ = fit(directory, model_type='lightgbm')
    path = directory / 'model.txt'
    columns = details['preprocessing']['columns']
    names = [f'Column_{index}' for index in range(len(columns))]
    content = path.read_text(encoding='utf-8')
    assert 'feature_names=' + ' '.join(columns) in content
    content = content.replace('feature_names=' + ' '.join(columns), 'feature_names=' + ' '.join(names))
    path.write_bytes(content.encode('utf-8'))
    details.update(engine='qlib.LGBModel', backend='qlib', native_model_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    details.pop('native_feature_names')
    if explicit_names:
        details['native_feature_names'] = names
    (directory / 'model.json').write_text(json.dumps(details), encoding='utf-8')
    np.testing.assert_allclose(predict_saved(directory, test), scores, rtol=1e-12, atol=1e-12)
    # A valid hash does not authorize a permutation of the native mapping.
    swapped = [names[1], names[0], *names[2:]]
    path.write_bytes(content.replace('feature_names=' + ' '.join(names), 'feature_names=' + ' '.join(swapped)).encode('utf-8'))
    details['native_model_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    details['native_feature_names'] = swapped
    (directory / 'model.json').write_text(json.dumps(details), encoding='utf-8')
    with pytest.raises(ValueError, match='feature order'):
        predict_saved(directory, test)


def test_future_features_and_test_labels_cannot_change_fitted_mlp(tmp_path):
    train, valid, test, columns = sample()
    options = dict(device='cpu', max_epochs=5, patience=3, batch_size=32)
    left_scores, left = fit_predict(train, valid, test, columns, model_type='mlp', backend='local',
        seed=42, directory=tmp_path / 'left', root=tmp_path, model_options=options)
    mutated = test.copy()
    mutated.loc[mutated.index[-5:], columns[:-1]] = 1e10
    mutated['label'] = -9999.
    right_scores, right = fit_predict(train, valid, mutated, columns, model_type='mlp', backend='local',
        seed=42, directory=tmp_path / 'right', root=tmp_path, model_options=options)
    assert left['preprocessing'] == right['preprocessing']
    assert left['learning_curve'] == right['learning_curve']
    assert left['best_epoch'] == right['best_epoch']
    with np.load(tmp_path / 'left/weights.npz', allow_pickle=False) as a, np.load(tmp_path / 'right/weights.npz', allow_pickle=False) as b:
        for key in a.files:
            np.testing.assert_array_equal(a[key], b[key])
    np.testing.assert_allclose(left_scores[:-5], right_scores[:-5], rtol=0, atol=0)


def test_qlib_requested_mlp_reports_main_torch_engine_without_qlib_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(models.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('MLP must not invoke Qlib worker'))
    (_, details), _, _ = fit(tmp_path / 'model', backend='qlib')
    assert details['backend'] == 'qlib'
    assert details['execution_backend'] == 'local_torch'
    assert details['engine'] == 'torch.MLP(shared_qlib_feature_contract)'
    assert 'not used' in details['backend_note']


def test_requested_unavailable_cuda_fails_without_fallback(tmp_path, monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    with pytest.raises(ValueError, match='explicitly requested CUDA'):
        fit(tmp_path / 'unavailable', options={'device': 'cuda'})
    assert not (tmp_path / 'unavailable/weights.npz').exists()
    assert not (tmp_path / 'unavailable/model.json').exists()


def test_actual_cuda_training_can_reload_on_cpu(tmp_path):
    import torch
    if not torch.cuda.is_available():
        pytest.skip('No CUDA device on this test host')
    (scores, details), test, _ = fit(tmp_path / 'cuda', options={'device': 'cuda', 'max_epochs': 3})
    assert details['actual_device'].startswith('cuda')
    np.testing.assert_allclose(predict_saved(tmp_path / 'cuda', test), scores, rtol=1e-4, atol=1e-6)
    np.testing.assert_allclose(predict_saved(tmp_path / 'cuda', test, device='cuda'), scores, rtol=1e-6, atol=1e-7)


def test_saved_inference_never_fits_or_writes_and_restores_rng(tmp_path, monkeypatch):
    import torch
    (expected, _), test, _ = fit(tmp_path / 'saved')
    before = {path.name: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
              for path in (tmp_path / 'saved').iterdir()}
    rng = torch.random.get_rng_state().clone()
    monkeypatch.setattr(models, 'fit_transform', lambda *args: pytest.fail('Inference must not fit preprocessing'))
    monkeypatch.setattr(neural, 'fit_mlp', lambda *args, **kwargs: pytest.fail('Inference must not train'))
    np.testing.assert_allclose(predict_saved(tmp_path / 'saved', test), expected, rtol=1e-6, atol=1e-7)
    assert torch.equal(rng, torch.random.get_rng_state())
    after = {path.name: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
             for path in (tmp_path / 'saved').iterdir()}
    assert before == after


def test_saved_native_weight_hash_and_preprocessing_schema_are_enforced(tmp_path):
    _, test, _ = fit(tmp_path / 'weights')
    native = tmp_path / 'weights/weights.npz'
    original = native.read_bytes()
    native.write_bytes(original + b'tamper')
    with pytest.raises(ValueError, match='native model SHA256'):
        predict_saved(tmp_path / 'weights', test)
    native.write_bytes(original)
    with pytest.raises(ValueError, match='columns mismatch'):
        predict_saved(tmp_path / 'weights', test.drop(columns=['feature_1']))
    preprocessing = tmp_path / 'weights/preprocessing.json'
    state = json.loads(preprocessing.read_text())
    state['columns'].reverse()
    preprocessing.write_text(json.dumps(state), encoding='utf-8')
    with pytest.raises(ValueError, match='preprocessing identity'):
        predict_saved(tmp_path / 'weights', test)


@pytest.mark.parametrize('options', [
    {'device': 'auto'}, {'device': 'mps'}, {'dropout': -.1}, {'learning_rate': 0},
    {'batch_size': True}, {'max_epochs': 1000}, {'typo': 1}, {'cpu_threads': 8},
])
def test_model_options_are_validated(options):
    with pytest.raises(ValueError):
        validate_model_options(options)


def test_cancellation_never_publishes_mlp_weights(tmp_path):
    train, valid, test, columns = sample()
    calls = []

    def cancel():
        calls.append(True)
        return len(calls) >= 4

    with pytest.raises(JobCancelled):
        fit_predict(train, valid, test, columns, model_type='mlp', backend='local', seed=42,
                    root=tmp_path, directory=tmp_path / 'cancel', check_cancel=cancel)
    assert not (tmp_path / 'cancel/model.json').exists()
    assert not (tmp_path / 'cancel/weights.npz').exists()
