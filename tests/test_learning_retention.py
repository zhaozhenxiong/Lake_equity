"""Exact retirement of completed Qlib exchange files, never model/data artifacts."""
import hashlib
import json
from pathlib import Path
import sqlite3
import os
import shutil
from datetime import datetime,timedelta,timezone

import numpy as np
import pandas as pd
import pytest

from lake_equity.learning.registry import canonical, digest
from lake_equity.learning.models import predict_saved


@pytest.fixture
def retained_run(tmp_path):
    run_id = 'learn-' + 'a' * 32
    folder = tmp_path / 'var/learning/runs' / run_id
    model = folder / 'models/fold-0/legacy_ma_ridge'
    model.mkdir(parents=True)
    state = dict(columns=['a'], median={'a': 0.}, low={'a': -10.}, high={'a': 10.}, mean={'a': 0.}, scale={'a': 1.})
    raw = canonical(state).encode()
    (model / 'preprocessing.json').write_bytes(raw)
    details = dict(model_type='ridge', backend='qlib', engine='qlib.DatasetH+sklearn.Ridge', feature_count=1,
                   preprocessing=state, preprocessing_sha256=hashlib.sha256(raw).hexdigest(), coefficients=[.5], intercept=.1)
    (model / 'model.json').write_text(canonical(details), encoding='utf-8')
    for name in ('train.parquet','valid.parquet','test.parquet','scores.parquet'):
        (model / name).write_bytes((name * 40).encode())
    pd.DataFrame([dict(date='2026-10-22',symbol='600000.SH',a=2.,score=1.1)]).to_parquet(folder / 'dataset.parquet')
    pd.DataFrame([dict(date='2026-10-22',symbol='600000.SH',score=1.1)]).to_parquet(folder / 'predictions.parquet')
    artifacts={p.relative_to(folder).as_posix():dict(path=p.relative_to(folder).as_posix(),sha256=digest(p),size_bytes=p.stat().st_size)
               for p in folder.rglob('*') if p.is_file()}
    report=dict(artifacts=artifacts,benchmark={'large_ignored':'x'*200000},family='cross_sectional_return_v1',
                run_id=run_id,status='completed',spec=dict(backend='qlib'),sources={'read_ref':'b'*64})
    path=folder/'report.json';path.write_text(canonical(report),encoding='utf-8')
    with sqlite3.connect(tmp_path/'var/learning/runs.db') as conn:
        conn.execute('CREATE TABLE runs(run_id TEXT PRIMARY KEY,report_sha256 TEXT,summary TEXT)')
        conn.execute('INSERT INTO runs VALUES(?,?,?)',(run_id,digest(path),canonical({k:report[k] for k in ('run_id','status','family','spec')})))
    return tmp_path,run_id,folder,model


def test_candidates_are_read_only_and_only_completed_qlib_exchange_files(retained_run):
    from lake_equity.learning.retention import LearningRetentionService
    root,run_id,folder,model=retained_run
    before=sorted(str(p.relative_to(root)) for p in root.rglob('*'))
    result=LearningRetentionService(root).candidates()
    candidate=[row for row in result['items'] if row['status']=='candidate']
    assert {Path(row['path_rel']).name for row in candidate}=={'train.parquet','valid.parquet','test.parquet'}
    assert all(row['run_id']==run_id for row in candidate)
    assert not any('scores.parquet' in row['path_rel'] for row in result['items'])
    assert sorted(str(p.relative_to(root)) for p in root.rglob('*'))==before


def test_exact_review_retires_only_exchange_preserving_original_report_and_inference(retained_run):
    from lake_equity.learning.retention import LearningRetentionService
    root,run_id,folder,model=retained_run
    service=LearningRetentionService(root)
    items=[row['object_id'] for row in service.candidates()['items'] if row['status']=='candidate']
    original=(folder/'report.json').read_bytes()
    before=predict_saved(model,pd.DataFrame([{'a':2.}]))
    reviewed=service.review(items)
    result=service.execute(reviewed['plan_id'],reviewed['confirmation_token'],items,confirmed=True,maintenance_confirmed=True)
    assert result['status']=='completed' and result['removed_count']==3
    assert (folder/'report.json').read_bytes()==original
    assert (model/'scores.parquet').is_file() and (folder/'dataset.parquet').is_file()
    assert (folder/'predictions.parquet').is_file()
    np.testing.assert_array_equal(predict_saved(model,pd.DataFrame([{'a':2.}])),before)
    state=service.status(run_id)
    assert state['status']=='training_exchange_retired' and len(state['retired_files'])==3
    assert 'byte_complete_training_audit_of_retired_exchange' in state['capability_loss']


def test_building_reference_and_unknown_manifest_protect_the_whole_run(retained_run):
    from lake_equity.learning.retention import LearningRetentionService
    root,run_id,folder,model=retained_run
    checkpoint=root/'var/learning/studies/.study-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.building/checkpoint.json'
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(canonical({'status':'running','runs':{'protocol':run_id}}))
    result=LearningRetentionService(root).candidates()
    assert all(row['status']=='protected' for row in result['items'])
    assert all('building_or_checkpoint_reference' in row['reasons'] for row in result['items'])


def reviewed_service(fixture):
    from lake_equity.learning.retention import LearningRetentionService
    root,*_=fixture
    service=LearningRetentionService(root)
    ids=[row['object_id'] for row in service.candidates()['items'] if row['status']=='candidate']
    return service,ids,service.review(ids)


def execute(service,ids,review,**kwargs):
    return service.execute(review['plan_id'],review['confirmation_token'],ids,
                           confirmed=True,maintenance_confirmed=True,**kwargs)


def test_token_and_exact_set_cannot_be_reused_for_different_objects(retained_run):
    service,ids,review=reviewed_service(retained_run)
    with pytest.raises(ValueError,match='exact reviewed'):
        execute(service,ids[:-1],review)
    with pytest.raises(ValueError,match='token'):
        execute(service,ids,{**review,'confirmation_token':'wrong'})
    assert all((retained_run[2]/row['artifact_path']).exists() for row in review['items'])


def test_changed_reference_or_same_length_corrupt_exchange_prevents_all_removals(retained_run):
    service,ids,review=reviewed_service(retained_run)
    item=review['items'][-1];path=retained_run[0]/item['path_rel'];info=path.stat()
    content=path.read_bytes();path.write_bytes(b'X'+content[1:]);os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
    result=execute(service,ids,review)
    assert result['status']=='failed' and result['removed_count']==0
    assert all((retained_run[0]/row['path_rel']).exists() for row in review['items'])


def test_new_checkpoint_reference_invalidates_previously_ready_plan(retained_run):
    service,ids,review=reviewed_service(retained_run)
    path=retained_run[0]/'var/learning/studies/.study-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.building/checkpoint.json'
    path.parent.mkdir(parents=True);path.write_text(canonical({'run_id':retained_run[1]}))
    result=execute(service,ids,review)
    assert result['status']=='failed' and result['removed_count']==0


def test_external_disappearance_after_verification_is_not_claimed_as_our_retirement(retained_run):
    service,ids,review=reviewed_service(retained_run)
    second=retained_run[0]/review['items'][1]['path_rel']
    def progress(value):
        if value.get('phase')=='retire_verified_exchange' and value.get('current')==1:second.unlink()
    result=execute(service,ids,review,progress=progress)
    assert result['status']=='failed' and result['removed_count']==1
    assert len(service.status(retained_run[1])['retired_files'])==1


def test_cancelled_partial_plan_can_resume_without_rewriting_the_original_report(retained_run):
    service,ids,review=reviewed_service(retained_run);stopped=False
    original=(retained_run[2]/'report.json').read_bytes()
    def progress(value):
        nonlocal stopped
        if value.get('phase')=='retire_verified_exchange':stopped=True
    result=execute(service,ids,review,progress=progress,check_cancel=lambda:stopped)
    assert result['status']=='cancelled' and result['removed_count']==1
    result=execute(service,ids,review)
    assert result['status']=='completed' and result['removed_count']==3
    assert (retained_run[2]/'report.json').read_bytes()==original


def test_expired_intent_can_only_reconcile_past_removal_and_new_approval_is_needed_for_rest(retained_run,monkeypatch):
    from lake_equity.learning import retention
    service,ids,review=reviewed_service(retained_run)
    replace=os.replace;failed=False
    def crash(source,destination):
        nonlocal failed
        target=Path(destination)
        if not failed and target.parent.name=='runs' and target.parent.parent.name=='retention':
            failed=True;raise OSError('simulated audit-write interruption after unlink')
        return replace(source,destination)
    monkeypatch.setattr(os,'replace',crash)
    result=execute(service,ids,review)
    assert result['status']=='failed' and result['removed_count']==0
    assert sum((retained_run[0]/row['path_rel']).exists() for row in review['items'])==2
    class Later(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.now(tz)+timedelta(minutes=31)
    monkeypatch.setattr(retention,'datetime',Later)
    with pytest.raises(ValueError,match='expired'):execute(service,ids,review)
    result=service.reconcile(review['plan_id'],maintenance_confirmed=True)
    assert result['status']=='reconciled' and result['removed_count']==1
    assert sum((retained_run[0]/row['path_rel']).exists() for row in review['items'])==2
    assert len([row for row in service.candidates()['items'] if row['status']=='candidate'])==2


def test_missing_any_original_manifest_artifact_blocks_the_whole_run(retained_run):
    from lake_equity.learning.retention import LearningRetentionService
    (retained_run[3]/'scores.parquet').unlink()
    result=LearningRetentionService(retained_run[0]).candidates()
    assert result['items']==[] and result['unknown_runs']


def test_shared_hardlink_is_protected(retained_run,tmp_path):
    from lake_equity.learning.retention import LearningRetentionService
    os.link(retained_run[3]/'train.parquet',tmp_path/'outside-link')
    rows=LearningRetentionService(retained_run[0]).candidates()['items']
    target=next(row for row in rows if row['artifact_path'].endswith('/train.parquet'))
    assert target['status']=='protected' and target['estimated_reclaim_bytes']==0


def test_large_known_benchmark_is_deferred_and_fully_verified_before_any_retirement(retained_run):
    from lake_equity.learning.retention import LearningRetentionService
    root=retained_run[0]
    path=root/('var/learning/studies/.study-'+'b'*32+'.building/benchmarks/'+ 'c'*64+'.json')
    path.parent.mkdir(parents=True)
    payload=canonical(dict(account_scope='shared_cash_account',config={'top_k':1000},cost_model={'version':'flat_v1'},
        ledger=[{'note':'x'*(17*1024*1024)}],schema='lake_target_weight_backtest_v1',status='completed',run_id='benchmark-label')).encode()
    path.write_bytes(payload);path.with_suffix('.sha256').write_text('0'*64)
    service=LearningRetentionService(root);preview=service.candidates()
    assert not preview['references']['errors'] and len(preview['references']['deferred_benchmark_checks'])==1
    ids=[row['object_id'] for row in preview['items'] if row['status']=='candidate'];review=service.review(ids)
    result=execute(service,ids,review)
    assert result['status']=='failed' and result['removed_count']==0
    assert all((root/row['path_rel']).exists() for row in review['items'])


def test_reference_walk_never_enters_nested_directory_redirect(retained_run,tmp_path):
    from lake_equity.learning.retention import LearningRetentionService
    import subprocess
    outside=tmp_path.parent/(tmp_path.name+'-outside');outside.mkdir()
    (outside/'outside.json').write_text('not JSON; a safe walker must never read this')
    base=tmp_path/'var/learning/studies/.study-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.building';base.mkdir(parents=True)
    link=base/'redirect'
    if os.name=='nt':
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True)
        if result.returncode:pytest.skip('Junction creation unavailable')
    else:link.symlink_to(outside,target_is_directory=True)
    preview=LearningRetentionService(tmp_path).candidates()
    assert preview['references']['errors'] and 'reparse' in preview['references']['errors'][0]['error']
    assert all(row['status']=='protected' for row in preview['items'])


def test_allocated_bytes_or_full_file_identity_changes_invalidate_the_exact_plan(retained_run,monkeypatch):
    from lake_equity.learning import retention
    service,ids,review=reviewed_service(retained_run)
    original=retention.file_measurement
    def changed(path,info):
        identity,allocated,method=original(path,info)
        return identity,allocated+4096,method
    monkeypatch.setattr(retention,'file_measurement',changed)
    result=execute(service,ids,review)
    assert result['status']=='failed' and result['removed_count']==0


def test_cli_mode_cannot_bypass_a_live_server_gate(retained_run):
    from filelock import FileLock,Timeout
    service,ids,review=reviewed_service(retained_run)
    with FileLock(str(retained_run[0]/'var/server.lock'),timeout=0):
        with pytest.raises(Timeout):execute(service,ids,review)
    assert all((retained_run[0]/row['path_rel']).is_file() for row in review['items'])


@pytest.mark.parametrize('legacy_problem',['missing_preprocessing_sha256','feature_count_mismatch','preprocessing_bytes_mismatch'])
def test_legacy_unloadable_model_preserves_entire_run_in_candidate_preview(retained_run,legacy_problem):
    from lake_equity.learning.retention import LearningRetentionService
    root,run_id,folder,model=retained_run
    second=folder/'models/fold-1/legacy_ma_ridge'
    shutil.copytree(model,second)
    details=json.loads((second/'model.json').read_text(encoding='utf-8'))
    if legacy_problem=='missing_preprocessing_sha256':
        details.pop('preprocessing_sha256')
    elif legacy_problem=='feature_count_mismatch':
        details['feature_count']=2
    else:
        state=json.loads((second/'preprocessing.json').read_text(encoding='utf-8'))
        state['mean']['a']=1.
        (second/'preprocessing.json').write_text(canonical(state),encoding='utf-8')
    (second/'model.json').write_text(canonical(details),encoding='utf-8')
    path=folder/'report.json';report=json.loads(path.read_text(encoding='utf-8'))
    report['artifacts']={p.relative_to(folder).as_posix():dict(path=p.relative_to(folder).as_posix(),
        sha256=digest(p),size_bytes=p.stat().st_size) for p in folder.rglob('*') if p.is_file() and p!=path}
    path.write_text(canonical(report),encoding='utf-8')
    with sqlite3.connect(root/'var/learning/runs.db') as conn:
        conn.execute('UPDATE runs SET report_sha256=? WHERE run_id=?',(digest(path),run_id))
    before=path.read_bytes()
    result=LearningRetentionService(root).candidates()
    assert result['items']==[]
    assert len(result['unknown_runs'])==1 and result['unknown_runs'][0]['run_id']==run_id
    assert 'preprocessing' in result['unknown_runs'][0]['reason'].lower()
    assert path.read_bytes()==before
    assert all((parent/name).is_file() for parent in (model,second)
               for name in ('train.parquet','valid.parquet','test.parquet'))
