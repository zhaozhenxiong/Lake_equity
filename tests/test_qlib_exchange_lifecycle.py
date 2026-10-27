"""New Qlib exchanges retire only after a valid model delivery, never on failure."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lake_equity.jobs import JobCancelled
from lake_equity.learning import models


@pytest.fixture
def worker(tmp_path, monkeypatch):
    executable = tmp_path / 'var/runtimes/qlib/Scripts/python.exe'
    executable.parent.mkdir(parents=True)
    executable.touch()
    outcome = {'mode': 'success', 'started': False, 'terminated': False}

    class Process:
        def __init__(self, command, **kwargs):
            outcome['started'] = True
            directory = Path(command[-1])
            test = pd.read_parquet(directory / 'test.parquet')
            prediction = test[['date', 'symbol']].copy()
            prediction['score'] = np.nan if outcome['mode'] == 'nonfinite' else .1
            if outcome['mode'] == 'wrong_keys':
                prediction.loc[prediction.index[0], 'symbol'] = '000002.SZ'
            prediction.to_parquet(directory / 'scores.parquet', index=False)
            details = dict(engine='qlib.DatasetH+sklearn.Ridge', coefficients=[0.], intercept=.1)
            if outcome['mode'] == 'invalid_model':
                details['coefficients'] = [0., 0.]
            (directory / 'model.json').write_text(json.dumps(details), encoding='utf-8')
            self.returncode = 1 if outcome['mode'] == 'failed' else 0

        def poll(self):
            return None if outcome['mode'] == 'cancelled' and not outcome['terminated'] else self.returncode

        def terminate(self):
            outcome['terminated'] = True

        def wait(self, timeout):
            return self.returncode

        def kill(self):
            outcome['terminated'] = True

    monkeypatch.setattr(models.subprocess, 'Popen', Process)
    frame = pd.DataFrame([dict(date=f'2026-09-{day:02d}', symbol='600000.SH', a=float(day), label=day / 100.)
                          for day in range(1, 29)])
    return tmp_path, outcome, frame.iloc[:14], frame.iloc[14:21], frame.iloc[21:]


def fit(worker, mode='success'):
    root, outcome, train, valid, test = worker
    outcome['mode'] = mode
    directory = root / 'new-fold'
    result = models.fit_predict(train, valid, test, ['a'], model_type='ridge', backend='qlib',
        seed=42, directory=directory, root=root,
        check_cancel=lambda: outcome['started'] and mode == 'cancelled')
    return directory, result, test


def test_successful_delivery_keeps_inference_and_scores_without_exchange_files(worker):
    directory, (scores, details), test = fit(worker)
    assert all(not (directory / name).exists() for name in ('train.parquet', 'valid.parquet', 'test.parquet'))
    assert (directory / 'scores.parquet').is_file() and (directory / 'request.json').is_file()
    assert details['worker_exchange_retention']['policy'] == 'ephemeral_after_verified_delivery_v1'
    np.testing.assert_array_equal(models.predict_saved(directory, test), scores)
    assert json.loads((directory / 'model.json').read_text())['worker_exchange_retention'] == details['worker_exchange_retention']


@pytest.mark.parametrize(('mode', 'error'), [
    ('failed', RuntimeError), ('nonfinite', ValueError), ('wrong_keys', ValueError),
    ('invalid_model', ValueError), ('cancelled', JobCancelled),
])
def test_failed_or_cancelled_delivery_preserves_all_exchange_inputs(worker, mode, error):
    with pytest.raises(error):
        fit(worker, mode)
    directory = worker[0] / 'new-fold'
    assert all((directory / name).is_file() for name in ('train.parquet', 'valid.parquet', 'test.parquet'))
    if mode == 'cancelled':
        assert worker[1]['terminated']
