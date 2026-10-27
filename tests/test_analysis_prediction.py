import hashlib
import json
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from lake_equity.research import ResearchBridge


@pytest.fixture
def bound_prediction(tmp_path, monkeypatch):
    from my_strategy.adapters import czsc_adapter
    # The immutable reader keeps a module-level from-import of load_bars. Import
    # it before installing a fixture loader so that teardown cannot leave that
    # temporary callable captured inside an otherwise unpatched module.
    from my_strategy.services import prediction_chart  # noqa: F401
    from my_strategy.services.kronos_prediction import prepare_input

    raw_db = tmp_path / 'var/research_inputs/current/khquant_raw.db'
    raw_db.parent.mkdir(parents=True)
    with sqlite3.connect(raw_db) as connection:
        connection.execute('CREATE TABLE synthetic_input(value INTEGER)')
        connection.execute('INSERT INTO synthetic_input VALUES(1)')
    manifest = {'manifest_id': 'synthetic-analysis-input', 'status': 'complete',
                'sqlite_sha256': hashlib.sha256(raw_db.read_bytes()).hexdigest()}
    raw_db.with_suffix('.manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    bridge = ResearchBridge(raw_db=raw_db, research_root=tmp_path / 'metadata/light_research',
                            results_db=tmp_path / 'results.db', runs_root=tmp_path / 'runs')
    dates = pd.bdate_range('2025-01-02', periods=50).strftime('%Y-%m-%d').tolist()
    prices = 10 + np.arange(40) * .01
    raw = pd.DataFrame(dict(symbol='000001.SZ', date=pd.to_datetime(dates[:40]), open=prices,
                            high=prices + .1, low=prices - .1, close=prices,
                            volume=1000., amount=prices * 1000., source='sina_unadjusted_v1', has_trade_price=1))
    raw.attrs.update(price_basis='unadjusted', volume_unit='shares', amount_unit='CNY', data_version='synthetic-prices')
    bars = [dict(time=row.date.strftime('%Y-%m-%d'), **{key: float(getattr(row, key))
            for key in ('open', 'high', 'low', 'close', 'volume')}, is_closed=True) for row in raw.itertuples()]
    analysis = bridge._persist('analysis', dict(symbol='000001.SZ', as_of=dates[39], data_end=dates[39],
                               price_basis='unadjusted', frequencies={'日线': {'bars': bars}}), manifest['manifest_id'])
    calls = []

    class Engine:
        def calendar(self):
            return dates, {'kind': 'synthetic-calendar'}

        def prediction(self, *, symbol, end, stop=None, raw_snapshot=None, **parameters):
            calls.append(dict(symbol=symbol, end=end, stop=stop, parameters=parameters))
            if stop:
                stop.check()
            frame = raw if raw_snapshot is None else raw_snapshot
            _, future, info = prepare_input(frame, [day for day in dates if day > end][:parameters.get('horizon', 5)],
                                            symbol=symbol, as_of=end, lookback=parameters.get('lookback', 128),
                                            horizon=parameters.get('horizon', 5))
            return {'symbol': symbol, 'as_of': end, 'input': info, 'forecast': [{'date': day, 'close': 11.,
                    'close_p10': 10.5, 'close_p90': 11.5} for day in future], 'quality': {'prediction_usable': True},
                    'identity': {'parameters': parameters}, 'result_ref': {'cache_key': 'a' * 64}}

    monkeypatch.setattr(czsc_adapter, 'load_bars', lambda *args, **kwargs: raw.copy())
    monkeypatch.setattr(bridge, '_engine', lambda: Engine())
    spec = dict(symbol='000001.SZ', end=dates[39], lookback=16, horizon=5,
                analysis_run_id=analysis['lake_run_id'], input_manifest_id=manifest['manifest_id'])
    return bridge, analysis, spec, raw, calls


def test_bound_prediction_preserves_analysis_identity_and_exact_tail(bound_prediction):
    bridge, analysis, spec, raw, calls = bound_prediction
    result = bridge.predict(spec)
    binding = result['analysis_binding']
    assert binding['compatible'] is True
    assert binding['analysis_run_id'] == analysis['lake_run_id']
    assert binding['input_manifest_id'] == spec['input_manifest_id']
    assert binding['lookback'] == 16
    assert binding['history_input_sha256'] == result['input']['data_sha256']
    assert len(binding['input_window_sha256']) == 64
    assert calls[0]['parameters'].get('analysis_run_id') is None
    assert bridge.read_report(result['lake_run_id'])['analysis_binding'] == binding


@pytest.mark.parametrize('change,reason', [
    ({'symbol': '600519.SH'}, 'symbol'), ({'end': '2025-02-25'}, 'cutoff'),
    ({'input_manifest_id': 'different-generation'}, 'manifest'), ({'lookback': 50}, 'history'),
])
def test_bound_prediction_rejects_mismatched_request_before_model(bound_prediction, change, reason):
    bridge, _, spec, _, calls = bound_prediction
    with pytest.raises(ValueError, match=reason):
        bridge.predict({**spec, **change})
    assert calls == []


@pytest.mark.parametrize('mutation,reason', [('price_basis', 'price'), ('window', 'window'), ('generation', 'manifest')])
def test_bound_prediction_rejects_changed_current_input(bound_prediction, mutation, reason):
    bridge, _, spec, raw, calls = bound_prediction
    if mutation == 'price_basis':
        raw.attrs['price_basis'] = 'qfq'
    elif mutation == 'window':
        raw.loc[raw.index[-1], 'close'] += .01
    else:
        path = bridge.raw_db.with_suffix('.manifest.json')
        value = json.loads(path.read_text())
        value['manifest_id'] = 'new-generation'
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match=reason):
        bridge.predict(spec)
    assert calls == []


def test_prediction_cancel_before_model_and_before_publication(bound_prediction, monkeypatch):
    bridge, _, spec, _, calls = bound_prediction
    with pytest.raises(InterruptedError):
        bridge.predict(spec, check_cancel=lambda: True)
    assert calls == []
    engine = bridge._engine()
    original = engine.prediction
    cancelled = False

    def infer(**kwargs):
        nonlocal cancelled
        result = original(**kwargs)
        cancelled = True
        return result

    monkeypatch.setattr(engine, 'prediction', infer)
    monkeypatch.setattr(bridge, '_engine', lambda: engine)
    with pytest.raises(InterruptedError):
        bridge.predict(spec, check_cancel=lambda: cancelled)
    assert len(bridge.list_reports()['items']) == 1


def test_prediction_checks_generated_input_before_publication(bound_prediction, monkeypatch):
    bridge, _, spec, _, _ = bound_prediction
    engine = bridge._engine()
    original = engine.prediction

    def infer(**kwargs):
        result = original(**kwargs)
        result['input']['data_sha256'] = 'b' * 64
        return result

    monkeypatch.setattr(engine, 'prediction', infer)
    monkeypatch.setattr(bridge, '_engine', lambda: engine)
    with pytest.raises(ValueError, match='prediction input'):
        bridge.predict(spec)
    assert len(bridge.list_reports()['items']) == 1


@pytest.fixture
def ths_bound_chart(bound_prediction, monkeypatch):
    from lake_equity import history
    from my_strategy.services import prediction_chart

    bridge, _, spec, raw, _ = bound_prediction
    raw['source'] = 'ths'
    raw['has_trade_price'] = 0
    saved = bridge.predict(spec)
    envelope = dict(schema='prediction_chart_v1', symbol=saved['symbol'], as_of=saved['as_of'],
                    input=deepcopy(saved['input']), parameters=deepcopy(saved['identity']['parameters']),
                    forecast=deepcopy(saved['forecast']), quality=deepcopy(saved['quality']), history=[],
                    history_verification=dict(status='unavailable', input_sha256_match=True,
                    reason_code='history_price_contract_unverified', reason='original whitelist boundary'))
    bridge.research_root.mkdir(parents=True, exist_ok=True)
    (bridge.research_root / 'research.db').touch()
    monkeypatch.setattr(prediction_chart, 'prediction_chart', lambda *args: deepcopy(envelope))
    monkeypatch.setattr(history, 'ReadOnlyResearchStore', lambda *args: SimpleNamespace(close=lambda: None))
    reader = history.HistoryReader(SimpleNamespace(raw_db=bridge.raw_db,
                     metadata_root=bridge.research_root.parent), bridge, None)
    return reader, saved, raw, envelope


def test_bound_ths_history_can_be_displayed_without_upgrading_original_quality(ths_bound_chart, monkeypatch):
    from my_strategy.services import kronos_prediction
    from my_strategy.services.light_research import LightResearch
    from my_strategy.storage.research_store import ResearchStore

    reader, saved, _, original = ths_bound_chart
    before = (reader.settings.raw_db.read_bytes(), reader.bridge.results_db.read_bytes())

    def forbidden(*args, **kwargs):
        raise AssertionError('A bound saved-chart GET started a model or mutable runtime')

    monkeypatch.setattr(LightResearch, '__init__', forbidden)
    monkeypatch.setattr(ResearchStore, '__init__', forbidden)
    monkeypatch.setattr(kronos_prediction, 'predict', forbidden)
    monkeypatch.setattr(reader.bridge, '_persist', forbidden)
    result = reader.prediction_chart('a' * 64, prediction_run_id=saved['lake_run_id'])
    assert len(result['history']) == 16
    assert result['history'][-1]['date'] == saved['as_of']
    assert result['history_verification']['analysis_binding_verified'] is True
    assert result['history_verification']['display_only_degraded'] is True
    assert result['quality'] == original['quality']
    assert result['forecast'] == original['forecast']
    assert (reader.settings.raw_db.read_bytes(), reader.bridge.results_db.read_bytes()) == before


def test_unbound_ths_history_remains_unavailable(ths_bound_chart):
    reader, _, _, original = ths_bound_chart
    assert reader.prediction_chart('a' * 64) == original


def test_bound_ths_revision_prevents_stitching_and_preserves_original_forecast(ths_bound_chart):
    reader, saved, raw, original = ths_bound_chart
    raw.loc[raw.index[-1], 'close'] += .01
    result = reader.prediction_chart('a' * 64, prediction_run_id=saved['lake_run_id'])
    assert result['history'] == []
    assert result['history_verification']['reason_code'] == 'analysis_binding_unverified'
    assert result['quality'] == original['quality']
    assert result['forecast'] == original['forecast']


def test_bound_chart_cannot_borrow_another_prediction_reference(ths_bound_chart):
    reader, saved, _, _ = ths_bound_chart
    with pytest.raises(ValueError, match='reference mismatch'):
        reader.prediction_chart('b' * 64, prediction_run_id=saved['lake_run_id'])


def test_running_prediction_observes_live_cancel_callback(bound_prediction, monkeypatch):
    bridge, _, spec, _, _ = bound_prediction
    cancelled = False
    engine = bridge._engine()

    def infer(**kwargs):
        nonlocal cancelled
        assert kwargs['stop'].is_set() is False
        cancelled = True
        kwargs['stop'].check()
        raise AssertionError('Cancelled inference must not continue')

    monkeypatch.setattr(engine, 'prediction', infer)
    monkeypatch.setattr(bridge, '_engine', lambda: engine)
    with pytest.raises(InterruptedError):
        bridge.predict(spec, check_cancel=lambda: cancelled)
    assert len(bridge.list_reports()['items']) == 1


def test_analysis_report_kind_and_closed_cutoff_are_required(bound_prediction):
    bridge, analysis, spec, _, calls = bound_prediction
    wrong = bridge._persist('prediction', analysis, spec['input_manifest_id'])
    with pytest.raises(ValueError, match='analysis report'):
        bridge.predict({**spec, 'analysis_run_id': wrong['lake_run_id']})
    forming = deepcopy(analysis)
    forming['frequencies']['日线']['bars'][-1]['is_closed'] = False
    saved = bridge._persist('analysis', forming, spec['input_manifest_id'])
    with pytest.raises(ValueError, match='closed daily bar'):
        bridge.predict({**spec, 'analysis_run_id': saved['lake_run_id']})
    assert calls == []


def test_input_manifest_change_during_prediction_prevents_publication(bound_prediction, monkeypatch):
    bridge, _, spec, _, _ = bound_prediction
    engine = bridge._engine()
    original = engine.prediction

    def infer(**kwargs):
        result = original(**kwargs)
        path = bridge.raw_db.with_suffix('.manifest.json')
        manifest = json.loads(path.read_text())
        manifest['modified_during_inference'] = True
        path.write_text(json.dumps(manifest))
        return result

    monkeypatch.setattr(engine, 'prediction', infer)
    monkeypatch.setattr(bridge, '_engine', lambda: engine)
    with pytest.raises(ValueError, match='changed while predicting'):
        bridge.predict(spec)
    assert len(bridge.list_reports()['items']) == 1


def test_unbound_model_page_prediction_remains_compatible_but_null_binding_is_rejected(bound_prediction):
    bridge, _, spec, _, calls = bound_prediction
    with pytest.raises(ValueError, match='analysis_run_id'):
        bridge.predict({**spec, 'analysis_run_id': None})
    assert calls == []
    unbound = {key: value for key, value in spec.items() if key != 'analysis_run_id'}
    result = bridge.predict(unbound)
    assert 'analysis_binding' not in result
    assert result['forecast']


def test_fixture_loader_does_not_replace_the_immutable_chart_reader(bound_prediction):
    from my_strategy.adapters import czsc_adapter
    from my_strategy.services import prediction_chart

    assert prediction_chart.load_bars is not czsc_adapter.load_bars
    assert prediction_chart.load_bars.__module__ == 'my_strategy.adapters.czsc_adapter'
