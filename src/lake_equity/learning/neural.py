"""Small return-regression MLP with data-only NumPy weight artifacts."""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np


MLP_DEFAULTS = dict(device='cpu', batch_size=2048, max_epochs=50, patience=6,
                    learning_rate=.001, weight_decay=.001, dropout=.1, cpu_threads=4)


def validate_model_options(options=None):
    if options is not None and not isinstance(options, dict):
        raise ValueError('MLP model_options must be an object')
    unknown = set(options or {}) - set(MLP_DEFAULTS)
    if unknown:
        raise ValueError('Unknown MLP model_options: ' + ', '.join(sorted(unknown)))
    result = {**MLP_DEFAULTS, **(options or {})}
    if result['device'] not in ('cpu', 'cuda'):
        raise ValueError('MLP device must be cpu or cuda')
    for key, maximum in (('batch_size', 8192), ('max_epochs', 100), ('patience', 20), ('cpu_threads', 4)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f'MLP {key} must be an integer in [1,{maximum}]')
    for key, high in (('learning_rate', .01), ('weight_decay', 1.), ('dropout', .5)):
        value = result[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not 0 <= value <= high or (key == 'learning_rate' and value == 0)):
            raise ValueError('Invalid MLP ' + key)
        result[key] = float(value)
    return result


def _device(torch, requested):
    if requested not in ('cpu', 'cuda'):
        raise ValueError('MLP device must be cpu or cuda')
    if requested == 'cuda' and not torch.cuda.is_available():
        raise ValueError('MLP explicitly requested CUDA, but CUDA is unavailable')
    return torch.device(requested)


def _network(torch, dimensions, dropout):
    return torch.nn.Sequential(torch.nn.Linear(dimensions, 64), torch.nn.GELU(),
                               torch.nn.Dropout(dropout), torch.nn.Linear(64, 32),
                               torch.nn.GELU(), torch.nn.Dropout(dropout), torch.nn.Linear(32, 1))


def _check(callback):
    if callback and callback():
        from lake_equity.jobs import JobCancelled
        raise JobCancelled('MLP training cancelled')


def _predict(torch, net, values, device, batch_size, check_cancel=None):
    net.eval()
    output = []
    with torch.inference_mode():
        for start in range(0, len(values), batch_size):
            _check(check_cancel)
            batch = torch.as_tensor(values[start:start + batch_size], dtype=torch.float32, device=device)
            output.append(net(batch).reshape(-1).cpu().numpy())
    return np.concatenate(output).astype(float) if output else np.empty(0, dtype=float)


def fit_mlp(train_x, train_y, valid_x, valid_y, test_x, *, seed, directory, options=None, check_cancel=None):
    import torch

    cfg = validate_model_options(options)
    device = _device(torch, cfg['device'])
    _check(check_cancel)
    train_x, valid_x, test_x = [np.asarray(array, dtype=np.float32) for array in (train_x, valid_x, test_x)]
    train_y, valid_y = [np.asarray(array, dtype=np.float32).reshape(-1) for array in (train_y, valid_y)]
    if not len(train_x) or not len(valid_x) or len(train_y) != len(train_x) or len(valid_y) != len(valid_x):
        raise ValueError('MLP requires nonempty aligned train and validation labels')
    if any(not np.isfinite(array).all() for array in (train_x, train_y, valid_x, valid_y, test_x)):
        raise ValueError('MLP requires finite preprocessed features and mature fit labels')
    if train_x.ndim != 2 or valid_x.shape[1:] != train_x.shape[1:] or test_x.shape[1:] != train_x.shape[1:]:
        raise ValueError('MLP feature dimensions differ')
    previous_threads = torch.get_num_threads()
    began, history = time.monotonic(), []
    cuda_devices = [torch.cuda.current_device()] if device.type == 'cuda' else []
    try:
        torch.set_num_threads(cfg['cpu_threads'])
        with torch.random.fork_rng(devices=cuda_devices):
            torch.random.default_generator.manual_seed(seed)
            if cuda_devices:
                torch.cuda.default_generators[cuda_devices[0]].manual_seed(seed)
            net = _network(torch, train_x.shape[1], cfg['dropout']).to(device)
            optimizer = torch.optim.AdamW(net.parameters(), lr=cfg['learning_rate'], weight_decay=cfg['weight_decay'])
            random = np.random.default_rng(seed)
            best_loss, best_epoch, best_state, stale = math.inf, 0, None, 0
            for epoch in range(1, cfg['max_epochs'] + 1):
                _check(check_cancel)
                net.train()
                permutation, total = random.permutation(len(train_x)), 0.
                for start in range(0, len(train_x), cfg['batch_size']):
                    _check(check_cancel)
                    indexes = permutation[start:start + cfg['batch_size']]
                    x = torch.as_tensor(train_x[indexes], device=device)
                    y = torch.as_tensor(train_y[indexes], device=device)
                    optimizer.zero_grad(set_to_none=True)
                    loss = torch.nn.functional.mse_loss(net(x).reshape(-1), y)
                    if not torch.isfinite(loss):
                        raise ValueError('Nonfinite MLP training loss')
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(net.parameters(), 1.)
                    optimizer.step()
                    total += float(loss.detach().cpu()) * len(indexes)
                prediction = _predict(torch, net, valid_x, device, cfg['batch_size'], check_cancel)
                valid_loss = float(np.mean(np.square(prediction - valid_y)))
                if not math.isfinite(valid_loss):
                    raise ValueError('Nonfinite MLP validation loss')
                history.append(dict(epoch=epoch, train_mse=total / len(train_x), valid_mse=valid_loss))
                if valid_loss < best_loss - 1e-12:
                    best_loss, best_epoch, stale = valid_loss, epoch, 0
                    best_state = {key: value.detach().cpu().clone() for key, value in net.state_dict().items()}
                else:
                    stale += 1
                if stale >= cfg['patience']:
                    break
            _check(check_cancel)
            net.load_state_dict(best_state, strict=True)
            scores = _predict(torch, net, test_x, device, cfg['batch_size'], check_cancel)
            destination = Path(directory) / 'weights.npz'
            np.savez_compressed(destination, **{key: value.numpy() for key, value in best_state.items()})
            details = dict(engine='torch.MLP', execution_backend='local_torch',
                architecture=dict(input_dimensions=train_x.shape[1], widths=[64, 32], output_dimensions=1,
                                  activation='GELU', dropout=cfg['dropout']),
                objective='future_return_regression_mse', label_units='fractional_return',
                options=cfg, requested_device=cfg['device'], actual_device=str(next(net.parameters()).device), seed=seed,
                best_epoch=best_epoch, selection='validation_mse_only', best_valid_mse=best_loss,
                epochs_completed=len(history), learning_curve=history,
                parameter_count=sum(value.numel() for value in best_state.values()),
                weights_format='numpy_npz_no_pickle', native_model_file='weights.npz',
                numeric_dtype='float32', torch_version=str(torch.__version__),
                duration_seconds=time.monotonic() - began)
            return scores, details
    finally:
        torch.set_num_threads(previous_threads)


def load_mlp(directory, details, *, device='cpu'):
    import torch

    target = _device(torch, device)
    architecture = details['architecture']
    if (architecture.get('widths') != [64, 32] or architecture.get('output_dimensions') != 1
            or architecture.get('activation') != 'GELU' or not 0 <= architecture.get('dropout', -1) <= .5
            or architecture.get('input_dimensions') != details['feature_count']):
        raise ValueError('Saved MLP architecture does not match its input schema')
    # Creating a loaded network must not consume the caller's training RNG.
    with torch.random.fork_rng(devices=[]):
        net = _network(torch, details['feature_count'], architecture['dropout'])
    expected = net.state_dict()
    with np.load(Path(directory) / 'weights.npz', allow_pickle=False) as arrays:
        if set(arrays.files) != set(expected):
            raise ValueError('Saved MLP weight keys differ from its architecture')
        state = {}
        for key, tensor in expected.items():
            value = arrays[key]
            if value.dtype != np.float32 or value.shape != tuple(tensor.shape) or not np.isfinite(value).all():
                raise ValueError('Saved MLP weight shape/dtype/values mismatch: ' + key)
            state[key] = torch.from_numpy(value.copy())
    net.load_state_dict(state, strict=True)
    net.to(target)
    def predict(values):
        return _predict(torch, net, np.asarray(values, dtype=np.float32), target, details['options']['batch_size'])
    return predict


def predict_mlp(directory, details, values, *, device='cpu'):
    return load_mlp(directory, details, device=device)(values)
