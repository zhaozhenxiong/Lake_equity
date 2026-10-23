"""Trusted in-memory Qlib worker. Inputs/outputs are Parquet and JSON only."""
from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path


def main(directory):
    import numpy as np
    import pandas as pd
    import qlib
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.data.dataset.loader import StaticDataLoader
    from qlib.workflow import R

    request = json.loads((directory / 'request.json').read_text(encoding='utf-8'))
    columns = request['columns']
    frames, segments = [], {}
    for name in ('train', 'valid', 'test'):
        frame = pd.read_parquet(directory / f'{name}.parquet')
        frame['datetime'] = pd.to_datetime(frame.date)
        indexed = frame.set_index(['datetime', 'symbol']).rename_axis(['datetime', 'instrument'])
        values = pd.concat({'feature': indexed[columns], 'label': indexed[['label']]}, axis=1)
        frames.append(values)
        segments[name] = (frame.datetime.min(), frame.datetime.max())
    data = pd.concat(frames).sort_index()
    if data.index.duplicated().any():
        raise ValueError('Duplicate Qlib dataset keys')
    provider = directory / 'empty_provider'
    provider.mkdir()
    qlib.init(provider_uri=str(provider), expression_cache=None, dataset_cache=None,
              exp_manager={'class': 'MLflowExpManager', 'module_path': 'qlib.workflow.expm',
                           'kwargs': {'uri': (directory / 'mlruns').as_uri(), 'default_exp_name': 'lake_learning'}})
    # Main process already fitted preprocessing on train and serialized its state.
    handler = DataHandlerLP(data_loader=StaticDataLoader(data), infer_processors=[],
                            learn_processors=[{'class': 'DropnaLabel'}])
    dataset = DatasetH(handler=handler, segments=segments)
    inference = dataset.prepare('test', col_set='feature', data_key=DataHandlerLP.DK_I)
    if request['model_type'] == 'lightgbm':
        from qlib.contrib.model.gbdt import LGBModel
        with R.start(experiment_name='lake_learning'):
            model = LGBModel(num_boost_round=160, early_stopping_rounds=20, num_threads=4,
                             deterministic=True, force_col_wise=True, seed=request['seed'],
                             feature_fraction_seed=request['seed'], learning_rate=.03,
                             num_leaves=15, max_depth=6, min_data_in_leaf=40, lambda_l2=5.0)
            model.fit(dataset, verbose_eval=0)
            scores = model.predict(dataset)
            model.model.save_model(str(directory / 'model.txt'))
            details = dict(engine='qlib.LGBModel', best_iteration=model.model.best_iteration,
                           importance=dict(zip(columns, model.model.feature_importance(importance_type='gain').tolist())))
    else:
        from sklearn.linear_model import Ridge
        train = dataset.prepare('train', col_set=['feature', 'label'], data_key=DataHandlerLP.DK_L)
        model = Ridge(alpha=10.0).fit(train['feature'].to_numpy(), train['label'].iloc[:, 0].to_numpy())
        scores = pd.Series(model.predict(inference.to_numpy()), index=inference.index)
        details = dict(engine='qlib.DatasetH+sklearn.Ridge', alpha=10.0, coefficients=model.coef_.tolist(),
                       intercept=float(model.intercept_), importance=dict(zip(columns, abs(model.coef_).tolist())))
    output = scores.rename('score').reset_index().rename(columns={'datetime': 'date', 'instrument': 'symbol'})
    output['date'] = output.date.dt.strftime('%Y-%m-%d')
    expected = pd.read_parquet(directory / 'test.parquet')[['date', 'symbol']]
    output = expected.merge(output, on=['date', 'symbol'], how='left', validate='one_to_one')
    if not np.isfinite(output.score).all():
        raise ValueError('Incomplete Qlib inference output')
    output.to_parquet(directory / 'scores.parquet', index=False)
    details['versions'] = {key: importlib.metadata.version(key) for key in ('pyqlib', 'numpy', 'pandas', 'lightgbm', 'scikit-learn')}
    (directory / 'model.json').write_text(json.dumps(details, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
