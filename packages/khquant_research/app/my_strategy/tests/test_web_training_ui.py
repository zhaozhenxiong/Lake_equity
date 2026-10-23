"""Verify the web training card: merged defaults, forced auto-replace, frozen plan start."""
import shutil
import subprocess

import pytest

from test_czsc_display_dates_ui import STATIC


HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm'),path=require('path');
const elements=new Map();
function element(id){if(!elements.has(id))elements.set(id,{id,value:'',hidden:false,disabled:false,checked:false,dataset:{},style:{},textContent:'',innerHTML:'',listeners:{},addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);}});return elements.get(id);}
global.document={getElementById:element,hidden:false,addEventListener(){}};
global.window={addEventListener(){},dispatchEvent(event){globalThis.lastSelectionEvent=event.type;}};
global.location={hash:'simulation'};
global.sessionStorage={getItem:()=>null};
global.Event=class{constructor(type){this.type=type;}};
const requests=[];let answer=async()=>{throw new Error('unexpected request');};
global.fetch=async(path,options={})=>{const request={path,method:options.method||'GET',body:options.body?JSON.parse(options.body):null};requests.push(request);const value=await answer(path,request);return {ok:true,status:200,text:async()=>JSON.stringify(value)};};
global.setTimeout=()=>1;global.clearTimeout=()=>{};
vm.runInThisContext(fs.readFileSync(path.join(process.argv[1],'web_training.js'),'utf8')
  .replace('globalThis.KHQuantWebTraining = {refresh};','globalThis.KHQuantWebTraining={refresh,preflight,start,state};'));
'''


def run_js(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for web training card checks')
    result = subprocess.run([node, '-e', HARNESS + '\n(async()=>{\n' + body +
                             '\n})().catch(error=>{console.error(error);process.exitCode=1;});', str(STATIC)],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_refresh_merges_saved_defaults_without_retired_auto_replace():
    run_js(r'''
answer=async path=>path.endsWith('/capabilities')?{schema:{fields:[{key:'device',default:'cpu'},{key:'cpu_workers',default:2},{key:'end',default:null}]},defaults:{device:'cpu'}}
 :path.endsWith('/profiles')?{default_profile:{parameters:{device:'mps',cpu_workers:4,auto_replace:false}}}
 :path.endsWith('/status')?{jobs:[],models:[],active_model:{run_id:'active-run',selected_at:'2026-10-18'}}
 :path.endsWith('/selection-events')?{items:[1,2,3,4,5,6].map(i=>({kind:'select',created_at:'2026-10-1'+i,payload:{run_id:'run-'+i,effective_from:'2026-10-1'+i,reason:'通过冻结证据'}}))}
 :null;
await KHQuantWebTraining.refresh();
assert.equal(KHQuantWebTraining.state.parameters.device,'mps');
assert.equal(KHQuantWebTraining.state.parameters.cpu_workers,4);
assert(!('auto_replace' in KHQuantWebTraining.state.parameters),'参考架构不接受旧训练参数：前端不再发送 auto_replace');
assert(element('web-training-status').innerHTML.includes('active-run'));
assert(!element('web-training-status').innerHTML.includes('尚无生效参考模型'));
const events=element('web-training-selection-events').innerHTML;
assert.equal((events.match(/<article>/g)||[]).length,5,'选择事件最多展示 5 条');
assert(events.includes('自动替换为当前模型'));assert(events.includes('生效 2026-10-11'));
assert(!events.includes('run-6'),'超出 5 条的旧事件不渲染');
''')


def test_preflight_and_start_submit_reference_parameters_and_track_replacement():
    run_js(r'''
answer=async path=>path.endsWith('/capabilities')?{schema:{fields:[{key:'device',default:'cpu'}]},defaults:{device:'cpu'}}
 :path.endsWith('/profiles')?{default_profile:{parameters:{}}}
 :path.endsWith('/status')?{jobs:[],models:[],active_model:{run_id:KHQuantWebTraining.state.jobs.size?'new-active-run':'active-run'}}
 :path.endsWith('/selection-events')?{items:[]}
 :path.endsWith('/preflight')?{valid:true,plan:{plan_id:'frozen-web-plan',historical_union_count:3200,incumbent_run_id:'active-run',calendar_run_id:'cal-1'}}
 :path.endsWith('/jobs')?{job_id:'web-job-1',run_id:'wrapper-1',status:'queued'}
 :path==='/api/tasks/web-job-1'?{job_id:'web-job-1',run_id:'wrapper-1',status:'succeeded',progress:100,result:{selection:{status:'replaced'}}}
 :null;
await KHQuantWebTraining.preflight();
const preflight=requests.find(r=>r.path.endsWith('/preflight'));
assert(!('auto_replace' in preflight.body.parameters),'预检参数不含旧训练参数 auto_replace');
assert(element('web-training-preflight-result').innerHTML.includes('预检通过'));
assert(element('web-training-preflight-result').innerHTML.includes('3,200'));
assert(element('web-training-preflight-result').innerHTML.includes('frozen-web-plan'));
assert.equal(element('web-training-start').disabled,false);
await KHQuantWebTraining.start();
const job=requests.find(r=>r.path.endsWith('/jobs'));
assert.equal(job.body.plan_id,'frozen-web-plan');
assert(!('auto_replace' in job.body.parameters),'提交训练参数不含旧训练参数 auto_replace');
assert(element('web-training-jobs').innerHTML.includes('已自动替换'));
assert(element('web-training-status').innerHTML.includes('new-active-run'),'完成后刷新当前生效模型');
assert.equal(globalThis.lastSelectionEvent,'khquant-selection-updated');
assert(element('web-training-state').textContent.includes('web-job-1'));
assert.equal(element('web-training-start').disabled,true,'提交后需重新检查计划');
''')


def test_blocked_plan_and_archived_profile_cannot_start_training():
    run_js(r'''
answer=async path=>path.endsWith('/capabilities')?{schema:{fields:[{key:'device',default:'cpu'}]},defaults:{device:'cpu'}}
 :path.endsWith('/profiles')?{default_profile:{archived:true,parameters:{device:'mps'}}}
 :path.endsWith('/status')?{jobs:[],models:[],active_model:null}
 :path.endsWith('/selection-events')?{items:[]}
 :path.endsWith('/preflight')?{valid:false,errors:['缺少核验日历'],plan:{plan_id:'blocked-plan'}}
 :null;
await KHQuantWebTraining.refresh();
assert.equal(KHQuantWebTraining.state.parameters.device,'cpu','已归档默认配置不再使用');
assert(element('web-training-status').innerHTML.includes('尚无生效参考模型'));
assert(element('web-training-selection-events').innerHTML.includes('尚无自动替换事件'));
await KHQuantWebTraining.preflight();
assert(element('web-training-preflight-result').innerHTML.includes('训练计划被阻断'));
assert(element('web-training-preflight-result').innerHTML.includes('缺少核验日历'));
assert.equal(element('web-training-start').disabled,true);
await KHQuantWebTraining.start();
assert(!requests.some(r=>r.path.endsWith('/jobs')),'阻断计划不能提交训练');
assert(element('web-training-state').textContent.includes('重新检查训练计划'));
''')
