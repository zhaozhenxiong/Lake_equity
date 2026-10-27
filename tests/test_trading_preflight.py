from copy import deepcopy

import pytest

from lake_equity.learning import selection
from lake_equity.learning.trading import TradingService, PlanBlocked
from test_learning_fusion_trading import account_fixture  # noqa: F401


def prepare_fixture(account_fixture,monkeypatch):
    service,account,report=account_fixture
    report.update(family='fusion_scan_v1',created_at='2026-10-22T16:00:00+08:00',frozen_at='2026-10-21T16:00:00+08:00')
    report['release_identity']['created_at']='2026-10-21T17:00:00+08:00'
    from datetime import datetime
    monkeypatch.setattr(selection,'_clock',lambda:datetime.fromisoformat('2026-10-22T18:00:00+08:00'))
    return service,account,report


def test_preflight_is_readonly_and_blocks_user_exact_temporal_mismatch(account_fixture,monkeypatch):
    service,account,report=prepare_fixture(account_fixture,monkeypatch)
    snapshot=service.read_snapshot(account['latest_snapshot_id'])
    snapshot['as_of']='2026-10-26'
    original=service.read_account
    monkeypatch.setattr(service,'read_account',lambda value:{**original(value),'snapshot':deepcopy(snapshot)})
    monkeypatch.setattr(service,'read_snapshot',lambda value:deepcopy(snapshot))
    before=service.index.read_bytes()
    value=service.preflight({'account_id':account['account_id'],'source_id':report['run_id']})
    assert value['status']=='blocked'
    assert value['dates']['snapshot_as_of']=='2026-10-26'
    assert any(row['code']=='account_signal_date_mismatch' for row in value['issues'])
    assert service.index.read_bytes()==before
    with pytest.raises(PlanBlocked):
        service.build_plan({'account_id':account['account_id'],'source_id':report['run_id']})
    assert service.list_plans()['plans']==[]


def test_preflight_ready_binds_source_and_latest_account_then_rechecks_at_save(account_fixture,monkeypatch):
    service,account,report=prepare_fixture(account_fixture,monkeypatch)
    params={'account_id':account['account_id'],'source_id':report['run_id']}
    value=service.preflight(params)
    assert value['status']=='ready'
    params.update(snapshot_id=value['identity']['snapshot_id'],source_sha256=value['identity']['source_sha256'])
    plan=service.build_plan(params)
    assert plan['source_id']==report['run_id']
    assert service.read_account(account['account_id'])['latest_snapshot_id']==account['latest_snapshot_id']
    params['source_sha256']='0'*64
    with pytest.raises(PlanBlocked):service.build_plan(params)


def test_expired_or_future_model_time_never_becomes_current_by_rolling_a_date(account_fixture,monkeypatch):
    service,account,report=prepare_fixture(account_fixture,monkeypatch)
    from datetime import datetime
    monkeypatch.setattr(selection,'_clock',lambda:datetime.fromisoformat('2026-10-26T12:00:00+08:00'))
    value=service.preflight({'account_id':account['account_id'],'source_id':report['run_id']})
    assert value['status']=='blocked' and value['dates']['execution_date']=='2026-10-23'
    assert any(row['code']=='signal_not_current' for row in value['issues'])


def test_waiting_for_account_write_lock_rechecks_the_window_inside_the_transaction(account_fixture,monkeypatch):
    service,account,report=prepare_fixture(account_fixture,monkeypatch)
    from datetime import datetime
    moment=[datetime.fromisoformat('2026-10-22T18:00:00+08:00')]
    monkeypatch.setattr(selection,'_clock',lambda:moment[0])
    connect=service._connect
    def after_wait():
        connection=connect()
        moment[0]=datetime.fromisoformat('2026-10-23T09:30:01+08:00')
        return connection
    monkeypatch.setattr(service,'_connect',after_wait)
    with pytest.raises(PlanBlocked,match='保存期间'):
        service.build_plan({'account_id':account['account_id'],'source_id':report['run_id']})
    assert service.list_plans()['plans']==[]


def test_unproved_rebalance_calendar_and_unscored_holdings_block_the_plan(account_fixture,monkeypatch):
    service,account,report=prepare_fixture(account_fixture,monkeypatch)
    original=service.read_snapshot
    def read_snapshot(value):
        return {**original(value),'last_rebalance_date':'2026-01-01'}
    monkeypatch.setattr(service,'read_snapshot',read_snapshot)
    params={'account_id':account['account_id'],'source_id':report['run_id'],'risk':{'rebalance_sessions':100}}
    assert any(row['code'].startswith('rebalance_calendar_') for row in service.preflight(params)['issues'])
    monkeypatch.setattr(service,'read_snapshot',original)
    report['rows'][0].update(baseline_status='excluded',reason='insufficient_verified_history')
    assert any(row['code']=='holding_score_unavailable' for row in service.preflight({'account_id':account['account_id'],'source_id':report['run_id']})['issues'])


def test_plain_factor_source_uses_the_same_planner_without_faking_kronos(account_fixture,monkeypatch):
    service,account,fusion=prepare_fixture(account_fixture,monkeypatch)
    plain={**fusion,'run_id':'scan-'+'c'*32,'family':'model_scan_v1','kind':'scan',
           'rows':[dict(symbol='000001.SZ',score=-.1,rank=1,status='scored'),
                   dict(symbol='600000.SH',score=-.2,rank=2,status='scored')]}
    monkeypatch.setattr(selection.SelectionService,'read',lambda self,identity:deepcopy(plain))
    monkeypatch.setattr(selection.SelectionService,'quotes',lambda self,source,symbols:{symbol:dict(reference_close=10.,reference_date='2026-10-22',prior_volume=1_000_000.,atr_pct=.02,quote_usable=True) for symbol in symbols})
    before=service.read_snapshot(account['latest_snapshot_id'])
    plan=service.build_plan(dict(account_id=account['account_id'],source_id=plain['run_id']))
    assert plan['source_kind']=='model_scan_v1' and plan['fusion_id'] is None
    assert {row['side'] for row in plan['orders']}=={'BUY','SELL'}
    assert service.read_snapshot(account['latest_snapshot_id'])==before


def test_unusable_hold_quote_is_not_treated_as_a_sellable_mark(account_fixture,monkeypatch):
    service,account,report=prepare_fixture(account_fixture,monkeypatch)
    report['rows'][0]['quote_usable']=False
    assert any(row['code']=='holding_valuation_missing' for row in service.preflight(dict(account_id=account['account_id'],source_id=report['run_id']))['issues'])
