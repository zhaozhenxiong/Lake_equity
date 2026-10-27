import shutil
import subprocess
from pathlib import Path

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'


def run_module(source):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for frontend behavior checks')
    result = subprocess.run([node, '--input-type=module', '-e', source], cwd=FRONTEND,
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_storage_protection_merges_all_reason_fields_and_unknown_size():
    run_module(r"""
import assert from 'node:assert/strict';
import {storageProtectionReasons,storageLogicalBytes} from './core/storage-reclamation.js';
const row={status:'protected',logical_bytes:0,reasons:['recent_generation'],blocked_reasons:['learning_reference','recent_generation'],holds:['retained_lake_equity_learning_or_read_identity']};
assert.deepEqual(storageProtectionReasons(row),['recent_generation','learning_reference','retained_lake_equity_learning_or_read_identity']);
assert.equal(storageLogicalBytes({...row,reasons:['missing_file_manifest']}),null);
assert.equal(storageLogicalBytes({...row,reasons:['unreceipted_generation']}),null);
assert.equal(storageLogicalBytes({logical_bytes:0,reasons:[]}),0);
""")


def test_learning_page_sorts_release_estimate_without_logical_substitution():
    run_module(r"""
import assert from 'node:assert/strict';
import {learningRetentionPage} from './core/storage-reclamation.js';
const rows=[{object_id:'a',status:'candidate',logical_bytes:999,estimated_reclaim_bytes:0},{object_id:'b',status:'candidate',logical_bytes:10,estimated_reclaim_bytes:10},{object_id:'c',status:'unknown',logical_bytes:1000,estimated_reclaim_bytes:null}];
assert.deepEqual(learningRetentionPage(rows,{sort:'reclaim'}).rows.map(x=>x.object_id),['b','a','c']);
assert.equal(learningRetentionPage(rows,{status:'unknown'}).rows[0].estimated_reclaim_bytes,null);
assert.equal(learningRetentionPage(rows,{query:'b',page:2}).page,1);
""")


def test_learning_selection_only_accepts_exact_candidate_ids():
    run_module(r"""
import assert from 'node:assert/strict';
import {learningRetentionSelection} from './core/storage-reclamation.js';
const rows=[{object_id:'a',status:'candidate'},{object_id:'b',status:'protected'},{object_id:'c',status:'unknown'}];
assert.deepEqual(learningRetentionSelection(rows,new Set(['a'])),['a']);
assert.throws(()=>learningRetentionSelection(rows,new Set(['b'])),/可核验候选/);
assert.throws(()=>learningRetentionSelection(rows,new Set(['outside'])),/可核验候选/);
assert.throws(()=>learningRetentionSelection(rows,new Set()),/选择/);
""")


def test_learning_confirm_requires_explicit_exact_ready_unexpired_plan():
    run_module(r"""
import assert from 'node:assert/strict';
import {learningRetentionConfirmation} from './core/storage-reclamation.js';
const plan={plan_id:'p',confirmation_token:'t',status:'ready',expires_at:'2099-01-01T00:00:00Z',items:[{object_id:'exact-id'}]};
assert.throws(()=>learningRetentionConfirmation(plan,{}),/确认/);
const body=learningRetentionConfirmation(plan,{confirmed:true,maintenance_confirmed:true});
assert.deepEqual(body.object_ids,['exact-id']);assert.equal(body.plan_id,'p');assert(!('path' in body));
assert.throws(()=>learningRetentionConfirmation({...plan,status:'complete'},{confirmed:true,maintenance_confirmed:true}),/重新核验/);
assert.throws(()=>learningRetentionConfirmation({...plan,expires_at:'2000-01-01T00:00:00Z'},{confirmed:true,maintenance_confirmed:true}),/过期/);
assert.deepEqual(learningRetentionConfirmation({...plan,status:'cancelled',can_resume:true},{confirmed:true,maintenance_confirmed:true}).object_ids,['exact-id']);
""")


def test_learning_preview_keeps_unknown_net_and_no_automatic_confirmation():
    run_module(r"""
import assert from 'node:assert/strict';
import {learningRetentionOverviewHtml,learningRetentionPlanHtml} from './core/storage-reclamation.js';
const html=learningRetentionOverviewHtml({schema:'learning_retention_v1',items:[{object_id:'unsafe',status:'unknown',logical_bytes:1e12,estimated_reclaim_bytes:null,reasons:['references_incomplete']}],totals:{estimated_reclaim_bytes:null}});
assert(html.includes('未知'));assert(!html.includes('data-learning-confirm'));
const unknown=learningRetentionOverviewHtml({items:[],unknown_runs:[{run_id:'learn-unknown',reason:'unreadable'}],totals:{unknown_runs:1,estimated_reclaim_bytes:0}});assert(unknown.includes('未核验实验'));assert(unknown.includes('learn-unknown'));assert(unknown.includes('不计入已列候选估算'));
const plan={plan_id:'p',status:'ready',confirmation_token:'t',items:[{object_id:'id',logical_bytes:1234,estimated_reclaim_bytes:0}],expires_at:'2099-01-01T00:00:00Z'};
const ready=learningRetentionPlanHtml(plan);
assert(ready.includes('data-learning-confirm'));assert(!ready.includes(' checked'));
assert(ready.includes('维护'));assert(ready.includes('不可撤销'));
assert(!learningRetentionPlanHtml({...plan,expires_at:'2000-01-01T00:00:00Z'}).includes('data-learning-confirm'));
assert(learningRetentionPlanHtml({...plan,confirmation_token:undefined}).includes('只读重开'));
assert(learningRetentionPlanHtml({...plan,status:'failed'}).includes('可能已有部分文件被移除'));
assert(learningRetentionPlanHtml({...plan,status:'running',can_cancel:true}).includes('data-learning-cancel'));
assert(learningRetentionPlanHtml({...plan,status:'failed',can_resume:true}).includes('确认按原计划继续'));
assert(learningRetentionPlanHtml({...plan,status:'failed',expires_at:'2000-01-01T00:00:00Z',can_reconcile:true}).includes('data-learning-reconcile'));
assert(!learningRetentionPlanHtml({...plan,confirmation_token:'one-use-secret-test'}).includes('one-use-secret-test'));
assert(learningRetentionPlanHtml({...plan,status:'running',progress:{phase:'verify_exchange_sha256',bytes:2048}}).includes('2 KiB'));
""")


def test_storage_review_failure_and_expiry_keep_explicit_plan_context():
    run_module(r"""
import assert from 'node:assert/strict';
import {storageReviewHtml} from './core/data-ops.js';
const row={plan_id:'frozen',job_id:'review',status:'ready',confirmation_token:'t',objects:[{object_id:'exact',logical_bytes:123}],expires_at:'2000-01-01T00:00:00Z'};
const expired=storageReviewHtml(row,true);assert(expired.includes('frozen'));assert(expired.includes('过期'));assert(!expired.includes('data-storage-confirm'));
assert(storageReviewHtml({...row,status:'error',error:'permission denied'},true).includes('permission denied'));
assert(!storageReviewHtml({...row,expires_at:'2099-01-01T00:00:00Z',objects:[]},true).includes('data-storage-confirm'));
""")


def test_learning_api_calls_use_registered_endpoints_and_exact_bodies():
    run_module(r"""
import assert from 'node:assert/strict';
import {api} from './core/api.js';
const calls=[];globalThis.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.learningRetentionCandidates();await api.learningRetentionReview({object_ids:['id']});await api.learningRetentionConfirm({plan_id:'p',object_ids:['id'],confirmed:true,maintenance_confirmed:true});await api.learningRetentionPlan('p');
await api.learningRetentionCancel('p');await api.learningRetentionReconcile('p',{maintenance_confirmed:true});
assert.equal(calls[0].url,'/api/v1/data/learning-retention/candidates');assert.equal(calls[0].options.method || 'GET','GET');
assert.equal(calls[1].url,'/api/v1/data/learning-retention/reviews');assert.deepEqual(JSON.parse(calls[1].options.body),{object_ids:['id']});
assert.equal(calls[3].url,'/api/v1/data/learning-retention/plans/p');
assert.equal(calls[4].url,'/api/v1/data/learning-retention/plans/p/cancel');assert.deepEqual(JSON.parse(calls[4].options.body),{});
assert.equal(calls[5].url,'/api/v1/data/learning-retention/plans/p/reconcile');assert.deepEqual(JSON.parse(calls[5].options.body),{maintenance_confirmed:true});
""")
