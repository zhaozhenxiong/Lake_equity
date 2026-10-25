"""Regression tests to install with the post-study reconstruction patch."""
import json

import numpy as np
import pandas as pd
import pytest

from lake_equity.learning import dataset as dataset_module
from lake_equity.learning import service as service_module
from lake_equity.learning import study as study_module
from lake_equity.learning import registry as registry_module
from lake_equity.learning.registry import digest
from lake_equity.learning.study import StudyService, study_folds, validate_spec
from lake_equity.learning.workflow import LearningWorkflow


@pytest.fixture
def parent_contract(tmp_path, monkeypatch):
    calendar = pd.bdate_range('2017-01-02', periods=400).strftime('%Y-%m-%d').tolist()
    start, end = calendar[100], calendar[359]
    spec = validate_spec(dict(start=start, end=end, train_windows=[40, 100], valid_sessions=10,
        test_sessions=20, holdout_sessions=40, models=['ridge'], backend='local', seeds=[42], include_margin=False))
    spec['read_ref'] = 'a'*64
    study_id = 'study-'+'b'*32
    folder = tmp_path/'var/learning/studies'/study_id
    folder.mkdir(parents=True)
    records = [dict(date=day, symbol=symbol, row_id=symbol+':'+day, a=float(np.sin(number)*1e7))
               for symbol in ['600519.SH', '000001.SZ'] for number, day in enumerate(calendar[100:360])]
    data = pd.DataFrame(records)
    data.to_parquet(folder/'dataset.parquet', index=False)
    raw_path = tmp_path/'raw-fixture.parquet'
    pd.DataFrame(dict(trade_date=pd.to_datetime(calendar))).to_parquet(raw_path, index=False)
    prepared = dict(calendar=calendar, symbols=['000001.SZ', '600519.SH'], sources=dict(read_ref='a'*64),
        report=dict(start=start, end=end, rows=len(data)), universe=dict(universe_id='frozen-parent-universe'),
        feature_groups=dict(price_volume=['a'], legacy_ma=['a']),
        dataset_artifact=dict(path='dataset.parquet', sha256=digest(folder/'dataset.parquet')))
    (folder/'prepared.json').write_text(json.dumps(prepared), encoding='utf-8')
    protocol = dict(protocol_id='pv38-ridge-w40', model_name='price_volume_ridge', model_type='ridge',
                    train_sessions=40, feature_set='price_volume', factor_groups=['price_volume'], seed=42)
    parent = dict(study_id=study_id, status='completed', spec=spec, selected_protocol=protocol,
        read_ref='a'*64, best_run_id='learn-'+'c'*32, implementation_sha256={'original': 'frozen'})
    source = dict(folds=study_folds(calendar, spec, 40), artifacts={'dataset.parquet': {'sha256': prepared['dataset_artifact']['sha256']}})
    service = StudyService(tmp_path)
    monkeypatch.setattr(service, 'read_run', lambda identity: parent)
    monkeypatch.setattr(service, '_code_identity', lambda: {'reconstruction': 'versioned'})
    monkeypatch.setattr(service, '_calendar', lambda value: (calendar, 'a'*64))
    class Session:
        def __init__(self, *args):
            self.snapshot = {'read_ref': 'a'*64}
            self.store = None
        def source_files(self, *args):
            return {'files': [{'path': str(raw_path)}]}
        def verify(self):
            pass
    monkeypatch.setattr(dataset_module, 'DatasetSession', Session)
    monkeypatch.setattr(dataset_module, '_read', lambda *args, **kwargs: pd.DataFrame(
        dict(trade_date=pd.to_datetime(calendar), is_trading=True)))
    monkeypatch.setattr(service_module.LearningService, 'read_run', lambda *args: source)
    def forbidden_prepare(*args, **kwargs):
        raise AssertionError('Reconstruction must reuse the frozen parent panel instead of preparing a new prefix')
    monkeypatch.setattr(service, '_prepare', forbidden_prepare)
    captures, state = [], {}
    def fit(spec, selected, frozen, working, run_id, progress, check_cancel, **kwargs):
        folds = kwargs['folds_override']
        inputs = {}
        for fold in folds:
            frame = service._frame(working, frozen, fold['train_start'], fold['train_end'])
            inputs[fold['test_start']] = frame[['row_id', 'a']].copy()
        captures.append(dict(spec=spec, protocol=selected, prepared=frozen, folds=folds, inputs=inputs))
        state['run_id'] = run_id
        return {'run_id': run_id}
    monkeypatch.setattr(service, '_fit_protocol', fit)
    class Registry:
        def __init__(self, root):
            pass
        def list_models(self):
            return {'series': [{'run_id': state['run_id'], 'series_id': 'series-'+'d'*32}]}
    monkeypatch.setattr(study_module, 'ModelRegistry', Registry)
    monkeypatch.setattr(registry_module, 'ModelRegistry', Registry)
    monkeypatch.setattr(LearningWorkflow, 'backtest', lambda self, params, **kwargs: params)
    return dict(service=service, parent=parent, source=source, folder=folder, captures=captures,
                calendar=calendar, data=data, prepared=prepared)


def test_same_fold_inputs_are_exact_across_different_requested_ends(parent_contract):
    item = parent_contract
    service, calendar = item['service'], item['calendar']
    before = {path.name: digest(path) for path in item['folder'].iterdir() if path.is_file()}
    for first, last in [(150, 260), (170, 340)]:
        service.historical_backtest(dict(study_id=item['parent']['study_id'], start=calendar[first], end=calendar[last]))
    left, right = item['captures']
    assert left['spec']['start'] == right['spec']['start'] == item['parent']['spec']['start']
    assert left['prepared']['symbols'] == right['prepared']['symbols'] == item['prepared']['symbols']
    common = set(left['inputs']) & set(right['inputs'])
    assert common
    for day in common:
        pd.testing.assert_frame_equal(left['inputs'][day], right['inputs'][day], check_exact=True)
    assert before == {path.name: digest(path) for path in item['folder'].iterdir() if path.is_file()}
    assert left['protocol']['historical_input_identity']['parent_dataset_sha256'] == item['prepared']['dataset_artifact']['sha256']


def test_confirmation_fold_schedule_comes_from_original_series(parent_contract):
    item, calendar = parent_contract, parent_contract['calendar']
    item['service'].historical_backtest(dict(study_id=item['parent']['study_id'], start=calendar[150], end=calendar[359]))
    folds = item['captures'][0]['folds']
    originals = item['source']['folds']
    confirmation = next(row for row in originals if row['segment'] == 'confirmation')
    observed = next(row for row in folds if row['test_start'] == confirmation['test_start'])
    assert observed['train_start'] == confirmation['train_start']
    assert observed['valid_start'] == confirmation['valid_start']
    assert observed['source_schedule_fold'] == confirmation['fold']


def test_insufficient_parent_history_is_rejected_without_preparation(parent_contract):
    item, calendar = parent_contract, parent_contract['calendar']
    with pytest.raises(ValueError, match='earliest reconstructible signal'):
        item['service'].historical_backtest(dict(study_id=item['parent']['study_id'], start=calendar[149], end=calendar[200]))
    assert not item['captures']


def test_long_window_has_no_earlier_interval_than_original_oos(parent_contract):
    item, calendar = parent_contract, parent_contract['calendar']
    item['parent']['selected_protocol']['train_sessions'] = 100
    with pytest.raises(ValueError, match='earliest reconstructible signal is '+calendar[210]):
        item['service'].historical_backtest(dict(study_id=item['parent']['study_id'], start=calendar[209], end=calendar[260]))
    assert not item['captures']


@pytest.mark.parametrize('extra', [{'read_ref': 'e'*64}, {'end': '2099-01-01'}])
def test_widening_input_requires_a_new_contract(parent_contract, extra):
    item, calendar = parent_contract, parent_contract['calendar']
    params = {**dict(study_id=item['parent']['study_id'], start=calendar[150], end=calendar[340]), **extra}
    with pytest.raises(ValueError, match='explicit study input contract'):
        item['service'].historical_backtest(params)
    assert not item['captures']
