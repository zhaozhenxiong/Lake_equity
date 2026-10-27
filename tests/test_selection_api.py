from copy import deepcopy

from lake_equity.learning.selection import SelectionService
from lake_equity.learning.scan_explanations import ScanExplanationService
from lake_equity.learning.trading import TradingService
from test_api_integration import integrated_app  # noqa: F401


def test_date_mismatch_is_a_preview_and_never_creates_a_failed_job(integrated_app,monkeypatch):
    client,app,settings=integrated_app
    result=dict(schema='plan_preflight_v1',status='blocked',issues=[dict(code='account_signal_date_mismatch',message='账户 10-26 与信号 10-22 不一致')],
                dates=dict(snapshot_as_of='2026-10-26',signal_date='2026-10-22'),identity={})
    monkeypatch.setattr(TradingService,'preflight',lambda self,body:deepcopy(result))
    before=app.state.jobs.list(None)
    body={'account_id':'account-a','source_id':'fusion-a'}
    response=client.post('/api/v1/trading/plans/preflight',json=body)
    assert response.status_code==200 and response.json()['status']=='blocked'
    response=client.post('/api/v1/jobs',json={'kind':'trade_plan','params':body})
    assert response.status_code==409
    assert response.json()['detail']['issues'][0]['code']=='account_signal_date_mismatch'
    assert app.state.jobs.list(None)==before


def test_old_and_fused_explanations_keep_the_source_identities(integrated_app,monkeypatch):
    client,app,settings=integrated_app
    scan='scan-'+'a'*32;fusion='fusion-'+'b'*32
    base=dict(run_id=scan,model_id='model-one',read_ref='frozen-ref',as_of='2026-10-22')
    fused=dict(base,run_id=fusion,scan_id=scan,release_id='release-one',rows=[dict(symbol='600000.SH',factor_score=.123)])
    def read(self,identity):return deepcopy(fused if identity==fusion else base)
    monkeypatch.setattr(SelectionService,'read',read)
    monkeypatch.setattr(ScanExplanationService,'read',lambda self,identity,symbol:dict(status='saved',score=.123,source=dict(run_id=identity,symbol=symbol)))
    response=client.get(f'/api/v1/scans/{fusion}/explanations/600000.SH')
    assert response.status_code==200
    value=response.json()
    assert value['source']['run_id']==fusion and value['factor_source']['run_id']==scan
    assert value['source']['base_scan_id']==scan
    fused['read_ref']='wrong-generation'
    assert client.get(f'/api/v1/scans/{fusion}/explanations/600000.SH').status_code==400


def test_decision_account_cannot_borrow_another_accounts_snapshot(integrated_app,monkeypatch):
    client,app,settings=integrated_app
    monkeypatch.setattr(TradingService,'read_account',lambda self,identity:dict(account_id=identity,latest_snapshot_id='one'))
    monkeypatch.setattr(TradingService,'read_snapshot',lambda self,identity:dict(account_id='other-account',snapshot_id=identity))
    assert client.get('/api/v1/scans/scan-a/decisions?account_id=account-a&snapshot_id=other').status_code==400
