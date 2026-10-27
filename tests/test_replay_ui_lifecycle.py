from test_frontend_contract import run_module


SETUP = r"""
import assert from 'node:assert/strict';
import {mountKronosReplay} from './core/kronos-replay.js';
import {api} from './core/api.js';
const nodes=new Map(),urls=[],submissions=[];
function node(){return{innerHTML:'',textContent:'button',disabled:false,hidden:false,isConnected:true,dataset:{},events:{},addEventListener(name,fn){this.events[name]=fn;},removeEventListener(){},replaceChildren(){this.innerHTML='';},insertAdjacentHTML(){},querySelector(){return node();},closest(){return null;}};}
const fields={symbols:{value:''},days:{value:'20'},horizon:{value:'1'},samples:{value:'8'},device:{value:'auto'},max_calls:{value:'150'},budget_seconds:{value:'300'},end:{value:''}};
const form={...node(),elements:fields};
const host={...node(),querySelector(selector){if(selector==='form')return form;if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);}};
globalThis.FormData=class{get(name){return fields[name]?.value ?? null;}has(){return false;}};
globalThis.history={replaceState(...args){urls.push(args[2]);}};
globalThis.document={getElementById(){return null;}};
api.replayRuns=async()=>({runs:[]});api.watchlist=async()=>({symbols:[]});
api.submit=async(kind,params)=>{submissions.push({kind,params});return{id:'pending-job',kind:'kronos_replay',status:'queued'};};
api.replay=async id=>({experiment_id:id,status:'partial',rows:[]});
function button(id){return{...node(),dataset:id?{openReplay:id}:{}};}
const click=button=>host.events.click({target:{closest(){return button;}}});
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
const controller=new AbortController();
"""


def test_late_open_cannot_replace_a_newer_experiment_or_resume_the_old_one():
    run_module(SETUP + r"""
let resolveA,resolveB;
api.replay=id=>new Promise(resolve=>{if(id==='A')resolveA=resolve;else resolveB=resolve;});
const cleanup=await mountKronosReplay(host,{signal:controller.signal});
const pendingA=click(button('A')),pendingB=click(button('B'));
resolveB({experiment_id:'B',status:'partial',rows:[]});await pendingB;
resolveA({experiment_id:'A',status:'partial',rows:[]});await pendingA;
assert.equal(urls.at(-1),'#models?replay=B');assert(!nodes.get('[data-replay-result]').innerHTML.includes('>A<'));
await click(button());assert.equal(submissions.length,1);assert.equal(submissions[0].params.resume_id,'B');
cleanup();controller.abort();
""")


def test_resume_uses_frozen_experiment_and_budget_even_when_current_watchlist_is_empty():
    run_module(SETUP + r"""
const cleanup=await mountKronosReplay(host,{signal:controller.signal,params:new URLSearchParams({replay:'saved-partial'})});
assert.equal(fields.symbols.value,'');await click(button());
assert.deepEqual(submissions,[{kind:'kronos_replay',params:{resume_id:'saved-partial',max_calls:150,budget_seconds:300}}]);
assert.equal(urls.at(-1),'#models?replay_job=pending-job');cleanup();controller.abort();
""")


def test_new_job_url_can_restore_tracking_and_fast_completion_keeps_report_url():
    run_module(SETUP + r"""
api.watchlist=async()=>({symbols:['600519.SH']});api.replayPreview=async()=>({status:'ready',request:{symbols:['600519.SH'],read_ref:'frozen'}});
const cleanup=await mountKronosReplay(host,{signal:controller.signal});
await form.events.submit({preventDefault(){}});await nodes.get('[data-replay-start]').events.click();
assert.equal(urls.at(-1),'#models?replay_job=pending-job');
api.submit=async()=>({id:'fast-job',kind:'kronos_replay',status:'succeeded',result:{experiment_id:'fast-report'}});
api.replay=async id=>({experiment_id:id,status:'completed',rows:[]});
await nodes.get('[data-replay-start]').events.click();await flush();assert.equal(urls.at(-1),'#models?replay=fast-report');
cleanup();controller.abort();
""")


def test_late_list_success_cannot_overwrite_a_newer_page():
    run_module(SETUP + r"""
const cleanup=await mountKronosReplay(host,{signal:controller.signal});
let old,newer;api.replayRuns=(signal,values)=>new Promise(resolve=>{if(values.offset===0)old=resolve;else newer=resolve;});
const first=nodes.get('[data-replay-refresh]').events.click();nodes.get('[data-replay-next]').events.click();
newer({runs:[{experiment_id:'newest-page',status:'completed'}],total:11,has_more:false});await flush();
old({runs:[{experiment_id:'stale-page',status:'completed'}],total:40,has_more:true});await first;await flush();
assert(nodes.get('[data-replay-runs]').innerHTML.includes('newest-page'));assert(!nodes.get('[data-replay-runs]').innerHTML.includes('stale-page'));
cleanup();controller.abort();
""")


def test_old_list_and_preview_failures_do_not_overwrite_new_success():
    run_module(SETUP + r"""
api.watchlist=async()=>({symbols:['600519.SH']});
const cleanup=await mountKronosReplay(host,{signal:controller.signal});
let failList,successList;api.replayRuns=(signal,values)=>new Promise((resolve,reject)=>{if(values.offset===0)failList=reject;else successList=resolve;});
const older=nodes.get('[data-replay-refresh]').events.click();nodes.get('[data-replay-next]').events.click();
successList({runs:[{experiment_id:'kept-page',status:'completed'}],has_more:false});await flush();
failList(new Error('STALE_LIST_ERROR'));await older;await flush();assert(nodes.get('[data-replay-runs]').innerHTML.includes('kept-page'));
let failPreview,successPreview,calls=0;api.replayPreview=()=>new Promise((resolve,reject)=>{if(calls++===0)failPreview=reject;else successPreview=resolve;});
const p1=form.events.submit({preventDefault(){}}),p2=form.events.submit({preventDefault(){}});
successPreview({status:'ready',request:{newest:'preview'}});await p2;
failPreview(new Error('STALE_PREVIEW_ERROR'));await p1;
assert(!nodes.get('[data-replay-preview]').innerHTML.includes('STALE_PREVIEW_ERROR'));assert(nodes.get('[data-replay-preview]').innerHTML.includes('newest'));
cleanup();controller.abort();
""")
