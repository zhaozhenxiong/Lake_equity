"""Frozen candidate coverage, conditional funding and actual-fill reconciliation."""
from copy import deepcopy
import json

import pandas as pd
import polars as pl
import pytest

from lake_equity.learning import fusion as fusion_module
from lake_equity.learning import releases as releases_module
from lake_equity.learning import trading as trading_module
from lake_equity.learning.fusion import FusionService
from lake_equity.learning.trading import TradingService
from test_learning_dataset import lake, publish


@pytest.fixture
def fusion_fixture(tmp_path, monkeypatch):
    release = dict(release_id='release-' + '1' * 32, model_id='model-' + '2' * 32, risk={}, fusion={'mode': 'factor_baseline'})
    rows = [dict(symbol=symbol, score=3 - index, rank=index + 1, status='scored')
            for index, symbol in enumerate(['600000.SH', '000001.SZ', '688001.SH'])]
    scan = dict(run_id='scan-' + '3' * 32, kind='scan', model_id=release['model_id'], final_ranking=True,
                status='completed', rows=rows, read_ref='a' * 64, as_of='2026-10-22', requested_cutoff='2026-10-22')
    monkeypatch.setattr(releases_module.ReleaseRegistry, 'resolve', lambda self, params: deepcopy(release))
    monkeypatch.setattr(fusion_module.LearningWorkflow, 'read_run', lambda self, identity: deepcopy(scan))
    def market(self, requested_scan, symbols, lookback):
        assert requested_scan['read_ref'] == 'a' * 64
        quotes = {symbol: dict(reference_close=10., prior_volume=1_000_000., atr_pct=.02, reference_date='2026-10-22') for symbol in symbols}
        return {symbol: pd.DataFrame() for symbol in symbols}, quotes, ['2026-10-23', '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29'], {'calendar_recent': ['2026-10-21', '2026-10-22']}
    monkeypatch.setattr(FusionService, '_market_inputs', market)
    calls = []
    def forecast(self, frame, future, symbol, as_of, parameters, cancel):
        calls.append(symbol)
        if symbol == '000001.SZ':
            raise RuntimeError('fixture inference failure')
        return dict(cache_key='fixture-' + symbol, summary=dict(terminal_return_median=.02, terminal_return_p10=-.03),
                    quality={'prediction_usable': True}, identity={'assets': 'fixture'}, resource={'actual_device': 'cpu'})
    monkeypatch.setattr(FusionService, '_forecast', forecast)
    return tmp_path, release, scan, calls


def test_fusion_partial_resume_preserves_population_and_prior_reports(fusion_fixture):
    root, release, scan, calls = fusion_fixture
    service = FusionService(root)
    first = service.run(dict(scan_id=scan['run_id'], candidate_count=2, holding_symbols=['688001.SH'], max_calls=1))
    assert calls == ['688001.SH']  # Risk review receives priority over unheld candidates.
    assert first['coverage'] == dict(total=3, baseline_total=3, candidates=2, holdings=1, succeeded=1, unusable=0, failed=0, deferred=2)
    assert first['status'] == 'partial' and first['final_ranking'] is True
    assert len(first['baseline_rows']) == 3
    assert sum(row['selection_eligible'] for row in first['rows']) == 2  # lambda zero
    original_files = list((service.directory / first['run_id']).glob('attempt-*.json'))
    original_bytes = original_files[0].read_bytes()
    final = service.run(dict(resume_id=first['run_id'], max_calls=2))
    assert final['status'] == 'completed'
    assert final['coverage']['failed'] == 1
    assert final['total_calls'] == 3
    assert original_files[0].read_bytes() == original_bytes
    assert service.read_run(first['run_id']) == final
    assert len(list((service.directory / first['run_id']).glob('attempt-*.json'))) == 2
    with pytest.raises(ValueError, match='contract'):
        service.run(dict(resume_id=first['run_id'], candidate_count=1))


def test_kronos_filter_never_advertises_unpredicted_as_selected(fusion_fixture):
    root, release, scan, calls = fusion_fixture
    service = FusionService(root)
    report = service.run(dict(scan_id=scan['run_id'], candidate_count=3, mode='kronos_filter', max_calls=1))
    assert report['final_ranking'] is False
    assert len([row for row in report['rows'] if row['selection_eligible']]) == 1
    assert all(row['direction'] == 'WAIT' for row in report['rows'] if row['kronos_status'] == 'deferred')
    assert report['qualification']['kronos_pretraining_cutoff_verified'] is False


def test_fusion_rejects_another_model_or_changed_frozen_data(fusion_fixture):
    root, release, scan, calls = fusion_fixture
    with pytest.raises(ValueError, match='read_ref'):
        FusionService(root).run(dict(scan_id=scan['run_id'], read_ref='b' * 64))
    scan['model_id'] = 'model-' + '4' * 32
    with pytest.raises(ValueError, match='model disagree'):
        FusionService(root).run(dict(scan_id=scan['run_id']))


def test_cancelled_fusion_persists_deferred_status(fusion_fixture, monkeypatch):
    from my_strategy.services import kronos_prediction
    released = []
    monkeypatch.setattr(kronos_prediction, 'release_prediction_resources', lambda: released.append(True))
    root, release, scan, calls = fusion_fixture
    report = FusionService(root).run(dict(scan_id=scan['run_id']), check_cancel=lambda: True)
    assert report['status'] == 'cancelled'
    assert report['coverage']['deferred'] == 3
    assert not calls
    assert released == [True]


def test_real_lake_prediction_input_retains_exact_dates_units_and_frozen_read(lake):
    root, store, days, bars = lake
    snapshot = store.capture()
    signal = days[225].isoformat()
    scan = dict(read_ref=snapshot['read_ref'], as_of=signal)
    frames, quotes, following, evidence = FusionService(root)._market_inputs(scan, ['600000.SH'], 128)
    frame = frames['600000.SH']
    assert len(frame) == 128 and frame.date.iloc[-1] == signal
    assert frame.attrs['calendar_history_complete'] is True
    assert frame.attrs['snapshot_read_ref'] == snapshot['read_ref']
    assert frame.attrs['volume_unit'] == 'shares' and frame.attrs['amount_unit'] == 'CNY'
    assert following[0] == days[226].isoformat()
    assert quotes['600000.SH']['reference_close'] == pytest.approx(bars.iloc[225].close)
    assert evidence['calendar_recent'][-1] == signal


@pytest.mark.parametrize('failure', ['geometry', 'source', 'corporate_action', 'missing'])
def test_holding_quotes_reject_invalid_source_action_geometry_or_staleness(lake, failure):
    root, store, days, bars = lake
    changed = bars.copy()
    if failure == 'geometry':
        changed.loc[225, 'high'] = 1.
    elif failure == 'source':
        changed.loc[225, 'source'] = 'unverified_fixture_source'
    elif failure == 'corporate_action':
        changed['corporate_action_suspected'] = False
        changed.loc[225, 'corporate_action_suspected'] = True
    else:
        changed = changed.drop(index=225)
    publish(store, 'daily_bars', pl.from_pandas(changed))
    scan = dict(read_ref=store.capture()['read_ref'], as_of=days[225].isoformat())
    _, quotes, _, _ = FusionService(root)._market_inputs(scan, ['600000.SH'], 128)
    assert quotes['600000.SH']['reference_close'] is None
    assert quotes['600000.SH']['quote_usable'] is False
    assert quotes['600000.SH']['quote_reason']


def test_fusion_cannot_reuse_unfiltered_scan_for_dynamic_release(fusion_fixture, monkeypatch):
    from lake_equity.learning import universe as universe_module
    root, release, scan, calls = fusion_fixture
    release['universe'] = {'policy': {'version': 'fixture-policy'}}
    with pytest.raises(ValueError, match='no frozen release universe'):
        FusionService(root).run(dict(scan_id=scan['run_id']))
    scan['universe'] = {'manifest_path': 'fixture'}
    evidence = dict(policy=release['universe']['policy'], read_ref=scan['read_ref'], start=scan['as_of'], end=scan['as_of'])
    verified = []
    monkeypatch.setattr(universe_module, 'read_universe', lambda value: verified.append(value) or deepcopy(evidence))
    FusionService(root).run(dict(scan_id=scan['run_id'], max_calls=1, holding_symbols=['600001.SH']))
    assert verified
    evidence['policy'] = {'version': 'wrong'}
    with pytest.raises(ValueError, match='universe policy'):
        FusionService(root).run(dict(scan_id=scan['run_id']))
    evidence['policy'] = release['universe']['policy']
    evidence['read_ref'] = 'b' * 64
    with pytest.raises(ValueError, match='universe policy'):
        FusionService(root).run(dict(scan_id=scan['run_id']))


def test_pool_excluded_holdings_are_risk_only_not_new_buy_candidates(fusion_fixture):
    root, release, scan, calls = fusion_fixture
    result = FusionService(root).run(dict(scan_id=scan['run_id'], candidate_count=1, holding_symbols=['600001.SH'], max_calls=1))
    holding = next(row for row in result['rows'] if row['symbol'] == '600001.SH')
    assert holding['is_holding'] and not holding['is_candidate']
    assert holding['baseline_status'] == 'not_in_baseline'
    assert not holding['selection_eligible'] and holding['direction'] == 'WAIT'


def test_kronos_observation_cache_reuses_verified_result_and_rejects_tampering(tmp_path, monkeypatch):
    from my_strategy.services import kronos_prediction, kronos_assets
    monkeypatch.setattr(kronos_assets, 'asset_status', lambda: dict(ready=True, files=[{'sha256': 'fixture-pinned'}]))
    monkeypatch.setattr(kronos_prediction, '_source_identity', lambda: {'source': 'fixture'})
    calls = []
    def predict(*args, **kwargs):
        calls.append(kwargs['symbol'])
        return {'cache_key': 'fixture-prediction', 'summary': {}, 'quality': {}, 'resource': {'actual_device': 'cpu'}}
    monkeypatch.setattr(kronos_prediction, 'predict', predict)
    service = FusionService(tmp_path)
    frame = pd.DataFrame({'date': ['2026-10-22'], 'close': [10.]})
    frame.attrs.update(calendar_history_complete=True, snapshot_read_ref='a' * 64)
    first = service._forecast(frame, ['2026-10-23'], '600000.SH', '2026-10-22', {'device': 'cpu'}, None)
    assert service.list_runs() == {'reports': []}  # Forecast cache may exist before a run publishes.
    second = service._forecast(frame, ['2026-10-23'], '600000.SH', '2026-10-22', {'device': 'cpu'}, None)
    assert calls == ['600000.SH']
    assert second['resource']['reused_observation'] is True
    assert second['resource']['fresh_inference'] is False
    path = next((service.directory / 'predictions').glob('*.json'))
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='SHA256'):
        service._forecast(frame, ['2026-10-23'], '600000.SH', '2026-10-22', {'device': 'cpu'}, None)


@pytest.fixture
def account_fixture(tmp_path, monkeypatch):
    from datetime import datetime
    from lake_equity.learning import selection
    monkeypatch.setattr(selection, '_clock', lambda: datetime.fromisoformat('2026-10-22T18:00:00+08:00'))
    service = TradingService(tmp_path)
    account = service.create_account(dict(name='SYNTHETIC TEST ONLY', snapshot=dict(as_of='2026-10-22', cash=10_000., is_fixture=True,
                 permissions={'star': False, 'chinext': False}, positions=[dict(symbol='600000.SH', total_shares=1000, sellable_shares=1000,
                 buy_date='2026-10-21', average_cost=10.)])))
    release = dict(release_id='release-' + '1' * 32, model_id='model-' + '2' * 32,
                   created_at='2026-10-21T17:00:00+08:00',
                   risk=dict(top_k=1, max_total_weight=.8, max_stock_weight=.8, rebalance_sessions=5), fusion={'mode': 'factor_baseline'})
    fusion = dict(run_id='fusion-' + '3' * 32, final_ranking=True, status='completed', release_id=release['release_id'], release_identity=release,
                  family='fusion_scan_v1', created_at='2026-10-22T16:00:00+08:00', frozen_at='2026-10-21T16:00:00+08:00',
                  model_id=release['model_id'], read_ref='a' * 64, as_of='2026-10-22', next_session='2026-10-23',
                  market_evidence={'calendar_recent': ['2026-10-21', '2026-10-22']}, rows=[
                      dict(symbol='600000.SH', selection_eligible=False, fused_rank=None, reference_close=10., prior_volume=1_000_000., atr_pct=.02, kronos_status='deferred'),
                      dict(symbol='000001.SZ', selection_eligible=True, fused_rank=1, reference_close=10., prior_volume=1_000_000., atr_pct=.02, kronos_status='deferred')])
    monkeypatch.setattr(trading_module.FusionService, 'read_run', lambda self, run_id: deepcopy(fusion))
    return service, account, fusion


def test_account_template_and_import_are_explicit_and_immutable(account_fixture):
    service, account, fusion = account_fixture
    before = service.read_snapshot(account['latest_snapshot_id'])
    assert account['is_fixture'] and not account['broker_connected']
    state = {key: value for key, value in before.items() if key in {'as_of', 'cash', 'positions', 'permissions', 'source', 'is_fixture', 'last_rebalance_date', 'note'}}
    state['cash'] = 9000.
    new = service.add_snapshot(account['account_id'], state)
    assert service.read_snapshot(before['snapshot_id']) == before
    assert service.read_account(account['account_id'])['latest_snapshot_id'] == new['snapshot_id']
    assert TradingService.import_template()['snapshot']['source'] == 'manual_template_import'
    state['as_of'] = '2026-10-21'
    state['positions'][0]['buy_lots'][0]['date'] = '2026-10-20'
    with pytest.raises(ValueError, match='rewind'):
        service.add_snapshot(account['account_id'], state)


def test_snapshot_does_not_mark_same_day_purchase_sellable(tmp_path):
    with pytest.raises(ValueError, match=r'T\+1'):
        TradingService(tmp_path).create_account(dict(name='fixture', snapshot=dict(as_of='2026-10-22', cash=0, is_fixture=True,
             positions=[dict(symbol='600000.SH', total_shares=100, sellable_shares=100, buy_date='2026-10-22')])))


def test_plan_sale_dependent_buy_is_conditional_and_does_not_mutate_account(account_fixture):
    service, account, fusion = account_fixture
    plan = service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    assert service.read_plan(plan['plan_id']) == plan
    assert [row['side'] for row in plan['orders']] == ['SELL', 'BUY']
    buy = plan['orders'][1]
    assert buy['funding'] == 'conditional_sale_proceeds' and buy['depends_on_orders'] == [plan['orders'][0]['order_id']]
    assert buy['quantity'] % 100 == 0
    assert service.read_account(account['account_id'])['snapshot']['cash'] == 10_000.
    assert plan['qualification']['automatic_orders'] is False
    assert plan['signal_date'] < plan['execution_date']


def test_partial_actual_fills_deduplicate_and_same_day_buys_stay_locked(account_fixture):
    service, account, fusion = account_fixture
    plan = service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    sell, buy = plan['orders']
    fill = dict(external_fill_id='fixture-sell-1', order_id=sell['order_id'], date=plan['execution_date'], quantity=500, price=10., fees=5.)
    result = service.record_fills(plan['plan_id'], {'fills': [fill]})
    assert result['snapshot']['cash'] == 14_995.
    repeat = service.record_fills(plan['plan_id'], {'fills': [fill]})
    assert repeat['status'] == 'already_recorded'
    with pytest.raises(ValueError, match='Conflicting'):
        service.record_fills(plan['plan_id'], {'fills': [dict(fill, quantity=400)]})
    bought = service.record_fills(plan['plan_id'], {'fills': [dict(external_fill_id='fixture-buy-1', order_id=buy['order_id'], date=plan['execution_date'], quantity=500, price=10., fees=5.)]})
    positions = {row['symbol']: row for row in bought['snapshot']['positions']}
    assert positions['000001.SZ']['total_shares'] == 500
    assert positions['000001.SZ']['sellable_shares'] == 0
    assert positions['600000.SH']['sellable_shares'] == 500
    with pytest.raises(ValueError, match='exceed this plan'):
        service.record_fills(plan['plan_id'], {'fills': [dict(fill, external_fill_id='fixture-overfill', quantity=1000)]})


def test_fill_batch_is_atomic_when_cash_is_insufficient(account_fixture):
    service, account, fusion = account_fixture
    plan = service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    buy = next(row for row in plan['orders'] if row['side'] == 'BUY')
    before = service.read_account(account['account_id'])['snapshot']
    with pytest.raises(ValueError, match='available cash'):
        service.record_fills(plan['plan_id'], {'fills': [dict(external_fill_id='fixture-no-cash', order_id=buy['order_id'], date=plan['execution_date'], quantity=buy['quantity'], price=1000., fees=5.)]})
    assert service.read_account(account['account_id'])['snapshot'] == before


def test_plan_requires_current_holdings_and_permissions(account_fixture):
    service, account, fusion = account_fixture
    fusion['rows'] = [fusion['rows'][1]]
    with pytest.raises(ValueError, match='持仓'):
        service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    fusion['rows'].insert(0, dict(symbol='600000.SH', selection_eligible=False, fused_rank=None, reference_close=10., prior_volume=1_000_000., atr_pct=.02, kronos_status='deferred'))
    fusion['rows'][1]['symbol'] = '688001.SH'
    plan = service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    assert any(row['reason'] == 'account_board_permission_missing' for row in plan['holds'])
    assert not any(row['side'] == 'BUY' for row in plan['orders'])


def test_unknown_locked_inventory_is_not_assumed_available_next_day(account_fixture):
    service, account, fusion = account_fixture
    snapshot = account['snapshot']
    state = dict(as_of=snapshot['as_of'], cash=snapshot['cash'], is_fixture=True,
                 positions=[dict(symbol='600000.SH', total_shares=1000, sellable_shares=0)])
    service.add_snapshot(account['account_id'], state)
    plan = service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    assert not any(row['side'] == 'SELL' for row in plan['orders'])
    assert any(row['reason'] == 't_plus_one_or_frozen_inventory' for row in plan['holds'])


def test_missing_valuation_blocks_all_rebalance_quantities(account_fixture):
    service, account, fusion = account_fixture
    fusion['rows'][0]['reference_close'] = None
    # Missing valuation now blocks saving instead of creating a no-order plan.
    with pytest.raises(trading_module.PlanBlocked, match='参考价格'):
        service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    assert service.list_plans()['plans'] == []


def test_baseline_zero_weight_ignores_kronos_for_holdings_as_well(account_fixture):
    service, account, fusion = account_fixture
    fusion['rows'][0].update(selection_eligible=True, fused_rank=1, kronos_status='succeeded',
                              kronos={'summary': {'terminal_return_p10': -.90}})
    fusion['rows'][1].update(selection_eligible=False, fused_rank=None)
    baseline = service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    assert not any(row['reason'] == 'kronos_downside_risk_exit' for row in baseline['orders'])
    fusion['mode'] = 'kronos_filter'
    filtered = service.build_plan(dict(account_id=account['account_id'], fusion_id=fusion['run_id']))
    assert any(row['reason'] == 'kronos_downside_risk_exit' for row in filtered['orders'])


def test_read_only_trading_lists_do_not_create_storage(tmp_path):
    service = TradingService(tmp_path / 'absent')
    assert service.list_accounts() == {'accounts': []}
    assert service.list_plans() == {'plans': []}
    assert not (tmp_path / 'absent').exists()
