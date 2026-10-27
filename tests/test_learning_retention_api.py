"""Real maintenance-gate windows remain observable and cancellable over HTTP."""
import threading
import time
import os

from lake_equity.learning.retention import LearningRetentionService
from lake_equity.learning.trading import PlanBlocked  # Initialize the app's real exception before optional backend fixture stubs.
from test_learning_retention import retained_run
from test_api_integration import integrated_app


def test_real_gate_blocks_normal_writes_but_plan_poll_and_cancel_remain_available(
        retained_run,integrated_app,monkeypatch):
    client,app,settings=integrated_app
    prefix='/api/v1/data/learning-retention'
    candidate=client.get(prefix+'/candidates')
    assert candidate.status_code==200,candidate.text
    ids=[row['object_id'] for row in candidate.json()['items'] if row['status']=='candidate']
    review=client.post(prefix+'/reviews',json={'object_ids':ids}).json()
    entered=threading.Event();release=threading.Event()
    original=LearningRetentionService._hash
    def pause_hash(self,*args,**kwargs):
        entered.set()
        assert release.wait(10)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(LearningRetentionService,'_hash',pause_hash)
    body=dict(plan_id=review['plan_id'],confirmation_token=review['confirmation_token'],object_ids=ids,
              confirmed=True,maintenance_confirmed=True)
    accepted=client.post(prefix+'/confirm',json=body)
    assert accepted.status_code==202,accepted.text
    assert entered.wait(10) and app.state.operations.gate.closed
    try:
        assert client.post('/api/v1/jobs',json={'kind':'scan','params':{}}).status_code==409
        assert client.post('/api/v1/trading/accounts',json={}).status_code==409
        observed=client.get(prefix+'/plans/'+review['plan_id'])
        assert observed.status_code==200 and observed.json()['status']=='running'
        assert observed.json()['progress'].get('phase')=='verify_report_stream'
        cancelled=client.post(prefix+'/plans/'+review['plan_id']+'/cancel',json={})
        assert cancelled.status_code==200,cancelled.text
    finally:
        release.set()
    for _ in range(100):
        saved=client.get(prefix+'/plans/'+review['plan_id']).json()
        if saved['status']=='cancelled':break
        time.sleep(.02)
    assert saved['status']=='cancelled' and saved['removed_count']==0
    assert not app.state.operations.gate.closed


def test_unknown_review_path_and_extra_confirm_fields_are_rejected(retained_run,integrated_app):
    client,_,_=integrated_app
    prefix='/api/v1/data/learning-retention'
    bad=client.post(prefix+'/reviews',json={'object_ids':['../../data']})
    assert bad.status_code==400
    assert client.post(prefix+'/reviews',json={'object_ids':[],'path':'D:/'}).status_code==422
    run_id=retained_run[1]
    for route in ('/api/v1/reports/','/api/v1/learning/runs/'):
        assert 'retention' not in client.get(route+run_id).json()
        view=client.get(route+run_id+'?view=dashboard')
        assert view.status_code==200 and view.json()['retention']['status']=='not_retired'


def test_failed_retirement_reports_truthful_failed_job_and_preserves_plan_error(retained_run,integrated_app):
    client,app,_=integrated_app;prefix='/api/v1/data/learning-retention'
    ids=[row['object_id'] for row in client.get(prefix+'/candidates').json()['items'] if row['status']=='candidate']
    review=client.post(prefix+'/reviews',json={'object_ids':ids}).json()
    path=retained_run[0]/review['items'][-1]['path_rel'];info=path.stat();content=path.read_bytes()
    path.write_bytes(b'X'+content[1:]);os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
    response=client.post(prefix+'/confirm',json=dict(plan_id=review['plan_id'],confirmation_token=review['confirmation_token'],
        object_ids=ids,confirmed=True,maintenance_confirmed=True))
    assert response.status_code==202,response.text
    for _ in range(200):
        job=app.state.jobs.get(response.json()['id'])
        if job['status'] not in {'queued','running','cancelling'}:break
        time.sleep(.01)
    assert job['status']=='failed' and job['business_status']=='failed'
    plan=client.get(prefix+'/plans/'+review['plan_id']).json()
    assert plan['status']=='failed' and plan['removed_count']==0
    assert any(event.get('error') for event in plan['journal'])
    result=client.get('/api/v1/jobs/'+job['id']+'/result')
    assert result.status_code==200 and result.json()['status']=='failed'
