"""Chronological folds and train-only preprocessing shared by both backends."""
from __future__ import annotations

import json
import hashlib
import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .neural import validate_model_options


def check_stop(check_cancel):
    if check_cancel and check_cancel():
        from lake_equity.jobs import JobCancelled
        raise JobCancelled("Learning cancelled")


def rolling_folds(calendar, spec):
    dates = sorted({str(day)[:10] for day in calendar if spec['start'] <= str(day)[:10] <= spec['end']})
    train, valid, test = (spec[key] for key in ('train_sessions', 'valid_sessions', 'test_sessions'))
    starts = list(range(train + valid, len(dates), test))[-spec['max_folds']:]
    if not starts:
        raise ValueError(f"Need at least {train + valid + 1} verified sessions for these windows")
    folds = []
    for number, offset in enumerate(starts):
        folds.append(dict(fold=number, train_start=dates[offset-valid-train], train_end=dates[offset-valid-1],
                          valid_start=dates[offset-valid], valid_end=dates[offset-1],
                          test_start=dates[offset], test_end=dates[min(offset+test, len(dates))-1]))
    return folds


def fit_transform(train, valid, test, columns):
    """Discard only training-empty columns; never infer the schema from test."""
    values = train[columns].replace([np.inf, -np.inf], np.nan)
    kept = [column for column in columns if values[column].notna().any()]
    if not kept:
        raise ValueError('No observed training features in this fold')
    values = values[kept]
    median = values.median()
    low, high = values.quantile(.01), values.quantile(.99)
    filled = values.fillna(median).clip(low, high, axis=1)
    mean, scale = filled.mean(), filled.std(ddof=0).replace(0, 1).fillna(1)
    transformed = []
    for frame in (train, valid, test):
        copy = frame[['date', 'symbol', 'label']].copy()
        copy[kept] = ((frame[kept].replace([np.inf, -np.inf], np.nan).fillna(median)
                       .clip(low, high, axis=1) - mean) / scale).to_numpy()
        transformed.append(copy)
    state = dict(columns=kept, dropped_training_empty=[c for c in columns if c not in kept],
                 median=median.to_dict(), low=low.to_dict(), high=high.to_dict(),
                 mean=mean.to_dict(), scale=scale.to_dict(), fitted_rows=len(train),
                 fit_start=str(train.date.min()), fit_end=str(train.date.max()))
    return transformed, state


def fit_predict(train, valid, test, columns, *, model_type, backend, seed, directory, root,
                check_cancel=None, model_options=None):
    if model_type not in ('ridge', 'lightgbm', 'mlp') or backend not in ('local', 'qlib'):
        raise ValueError('Unsupported learning model_type or backend')
    if model_type != 'mlp' and model_options is not None:
        raise ValueError('model_options apply only to MLP')
    directory = Path(directory)
    directory.mkdir(parents=True)
    (train, valid, test), state = fit_transform(train, valid, test, columns)
    columns = state['columns']
    (directory / 'preprocessing.json').write_text(json.dumps(state, indent=2, allow_nan=False), encoding='utf-8')
    check_stop(check_cancel)
    if model_type == 'mlp':
        from .neural import fit_mlp
        scores, details = fit_mlp(train[columns].to_numpy(), train.label.to_numpy(),
            valid[columns].to_numpy(), valid.label.to_numpy(), test[columns].to_numpy(),
            seed=seed, directory=directory, options=model_options, check_cancel=check_cancel)
        details['data_contract'] = 'shared_qlib_train_only_preprocessing_v1'
        if backend == 'qlib':
            details['engine'] = 'torch.MLP(shared_qlib_feature_contract)'
            details['backend_note'] = 'Main Torch runtime; the isolated Qlib worker is not used for MLP'
    elif backend == 'qlib':
        for name, frame in [('train', train), ('valid', valid), ('test', test)]:
            frame.to_parquet(directory / f'{name}.parquet', index=False)
        request = dict(columns=columns, model_type=model_type, seed=seed)
        (directory / 'request.json').write_text(json.dumps(request), encoding='utf-8')
        python = Path(root) / 'var/runtimes/qlib' / ('Scripts/python.exe' if __import__('os').name == 'nt' else 'bin/python')
        if not python.is_file():
            raise ValueError('Qlib runtime missing; follow docs/LEARNING.md to install it')
        with (directory / 'worker.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(python), '-B', str(Path(__file__).with_name('qlib_worker.py')),
                                        str(directory.resolve())], stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            started = time.monotonic()
            try:
                while process.poll() is None:
                    check_stop(check_cancel)
                    if time.monotonic() - started > 900:
                        raise TimeoutError('Qlib fit exceeded its 900 second worker bound')
                    time.sleep(.15)
                if process.returncode:
                    raise RuntimeError(f"Qlib worker failed; inspect {directory / 'worker.log'}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        result = pd.read_parquet(directory / 'scores.parquet')
        if list(zip(result.date, result.symbol)) != list(zip(test.date, test.symbol)):
            raise ValueError('Qlib worker prediction keys differ from requested inference population')
        details = json.loads((directory / 'model.json').read_text(encoding='utf-8'))
        scores = result.score.to_numpy(float)
    elif model_type == 'ridge':
        from sklearn.linear_model import Ridge
        model = Ridge(alpha=10.0).fit(train[columns].to_numpy(), train.label.to_numpy())
        scores = model.predict(test[columns].to_numpy())
        details = dict(engine='sklearn.Ridge', alpha=10.0, coefficients=model.coef_.tolist(),
                       intercept=float(model.intercept_), importance=dict(zip(columns, abs(model.coef_).tolist())))
    else:
        import lightgbm as lgb
        model = lgb.train(dict(objective='regression', metric='l2', verbosity=-1, num_threads=4,
                               deterministic=True, force_col_wise=True, seed=seed, feature_fraction_seed=seed,
                               learning_rate=.03, num_leaves=15, max_depth=6, min_data_in_leaf=40,
                               lambda_l2=5.0),
                          lgb.Dataset(train[columns].to_numpy(), label=train.label.to_numpy(), feature_name=columns),
                          num_boost_round=160,
                          valid_sets=[lgb.Dataset(valid[columns].to_numpy(), label=valid.label.to_numpy())],
                          callbacks=[lgb.early_stopping(20, verbose=False), lambda env: check_stop(check_cancel)])
        scores = model.predict(test[columns].to_numpy())
        model.save_model(str(directory / 'model.txt'))
        details = dict(engine='lightgbm', best_iteration=model.best_iteration,
                       importance=dict(zip(columns, model.feature_importance(importance_type='gain').tolist())))
    check_stop(check_cancel)
    if not np.isfinite(scores).all():
        raise ValueError('Model returned nonfinite inference scores')
    native = details.get('native_model_file', 'model.txt' if model_type == 'lightgbm' else None)
    if native:
        details['native_model_file'] = native
        details['native_model_sha256'] = hashlib.sha256((directory / native).read_bytes()).hexdigest()
        if model_type == 'lightgbm':
            import lightgbm as lgb
            details['native_feature_names'] = lgb.Booster(model_file=str(directory / native)).feature_name()
    details.update(feature_count=len(columns), preprocessing=state, backend=backend, model_type=model_type,
                   preprocessing_sha256=hashlib.sha256((directory / 'preprocessing.json').read_bytes()).hexdigest())
    (directory / 'model.json').write_text(json.dumps(details, indent=2, allow_nan=False), encoding='utf-8')
    return scores, details


def _apply_saved_preprocessing(frame, state):
    columns = state.get('columns')
    if (not isinstance(columns, list) or not columns or len(set(columns)) != len(columns)
            or any(not isinstance(column, str) for column in columns) or frame.columns.duplicated().any()
            or not set(columns) <= set(frame.columns)):
        raise ValueError('Saved preprocessing input columns mismatch')
    stats = {}
    for key in ('median', 'low', 'high', 'mean', 'scale'):
        mapping = state.get(key)
        if not isinstance(mapping, dict) or set(mapping) != set(columns):
            raise ValueError('Saved preprocessing statistics columns mismatch: ' + key)
        stats[key] = np.asarray([mapping[column] for column in columns], dtype=float)
        if not np.isfinite(stats[key]).all():
            raise ValueError('Nonfinite saved preprocessing statistics')
    if (stats['scale'] <= 0).any() or (stats['low'] > stats['high']).any():
        raise ValueError('Invalid saved preprocessing scale or bounds')
    values = frame[columns].to_numpy(dtype=float)
    filled = np.where(np.isfinite(values), values, stats['median'])
    return (np.clip(filled, stats['low'], stats['high']) - stats['mean']) / stats['scale']


def load_saved(directory, *, device='cpu'):
    """Validate and load once; return a predictor reusable across symbol batches."""
    directory = Path(directory)
    details = json.loads((directory / 'model.json').read_text(encoding='utf-8'))
    raw = (directory / 'preprocessing.json').read_bytes()
    state = json.loads(raw)
    if (details.get('preprocessing') != state
            or details.get('preprocessing_sha256') != hashlib.sha256(raw).hexdigest()
            or details.get('feature_count') != len(state.get('columns', []))):
        raise ValueError('Saved preprocessing identity mismatch')
    _apply_saved_preprocessing(pd.DataFrame(columns=state.get('columns', [])), state)
    model_type = details.get('model_type')
    if model_type == 'ridge':
        if device != 'cpu':
            raise ValueError('Saved Ridge inference supports CPU only')
        coefficients = np.asarray(details['coefficients'], dtype=float)
        intercept = float(details['intercept'])
        if coefficients.shape != (len(state['columns']),) or not np.isfinite(coefficients).all() or not np.isfinite(intercept):
            raise ValueError('Saved Ridge coefficients mismatch')
        predict = lambda values: values @ coefficients + intercept
    elif model_type in ('lightgbm', 'mlp'):
        filename = 'weights.npz' if model_type == 'mlp' else 'model.txt'
        path = directory / filename
        if (details.get('native_model_file') != filename or path.is_symlink()
                or details.get('native_model_sha256') != hashlib.sha256(path.read_bytes()).hexdigest()):
            raise ValueError('Saved native model SHA256 mismatch')
        if model_type == 'mlp':
            from .neural import load_mlp
            predict = load_mlp(directory, details, device=device)
        else:
            if device != 'cpu':
                raise ValueError('Saved LightGBM inference supports CPU only')
            import lightgbm as lgb
            model = lgb.Booster(model_file=str(path))
            # Qlib's LGBModel converts its ordered feature frame to NumPy, so
            # LightGBM exports canonical positional names, not factor names.
            # v1 bundles without explicit names use that exact known mapping;
            # arbitrary names or permutations still fail closed.
            positional = [f'Column_{index}' for index in range(len(state['columns']))]
            known_names = positional if details.get('engine') == 'qlib.LGBModel' and details.get('backend') == 'qlib' else state['columns']
            expected_names = details.get('native_feature_names', known_names)
            if expected_names != known_names or model.num_feature() != len(state['columns']) or model.feature_name() != expected_names:
                raise ValueError('Saved LightGBM feature order mismatch')
            predict = model.predict
    else:
        raise ValueError('Unknown saved model type')
    def infer(frame):
        scores = predict(_apply_saved_preprocessing(frame, state))
        if np.asarray(scores).shape != (len(frame),) or not np.isfinite(scores).all():
            raise ValueError('Invalid saved inference predictions')
        return np.asarray(scores, dtype=float)
    return infer


def predict_saved(directory, frame, *, device='cpu'):
    """Load frozen model parameters and transform/predict without fitting."""
    return load_saved(directory, device=device)(frame)


def signal_metrics(predictions):
    frame = predictions[np.isfinite(predictions.label.to_numpy(float))].copy()
    daily = []
    for day, group in frame.groupby('date', sort=True):
        if len(group) < 3 or group.score.nunique() < 2 or group.label.nunique() < 2:
            continue
        ranked = group.sort_values(['score', 'symbol'])
        count = max(1, len(ranked) // 5)
        daily.append(dict(date=day, count=len(group), ic=float(group.score.corr(group.label)),
                          rank_ic=float(group.score.rank().corr(group.label.rank())),
                          top_minus_bottom=float(ranked.label.tail(count).mean()-ranked.label.head(count).mean())))
    result = dict(observations=len(frame), dates=int(frame.date.nunique()), daily=daily,
                  rmse=float(np.sqrt(np.mean((frame.score-frame.label)**2))) if len(frame) else None)
    for key in ('ic', 'rank_ic', 'top_minus_bottom'):
        result[key] = float(np.mean([row[key] for row in daily])) if daily else None
    return result
