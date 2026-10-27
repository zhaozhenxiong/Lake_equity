from test_frontend_contract import run_module


# A small DOM seam: replacing the host HTML really removes its controls.  Child
# content is kept separate, so an error slot cannot make these tests pass by
# accidentally replacing the whole component.
SETUP = r"""
import assert from 'node:assert/strict';
import {mountTrading} from './core/trading.js';
import {api} from './core/api.js';
import {showError} from './core/ui.js';
const urls=[],submissions=[];
class Element {
  constructor(){this.events={};this.dataset={};this.children=new Map();this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.isConnected=true;this._html='';this.textContent='button';this.options=[];this.elements={};}
  set innerHTML(value){this._html=value;if(this.isHost){this.children.clear();for(const tag of value.matchAll(/<[^>]+>/g)){const node=new Element();for(const match of tag[0].matchAll(/\b(data-[\w-]+)(?=[\s=>])/g)){node.setAttribute(match[1],"");if(!this.children.has(`[${match[1]}]`))this.children.set(`[${match[1]}]`,node);}for(const match of tag[0].matchAll(/\bname="([\w-]+)"/g))this.children.set(`[name=${match[1]}]`,node);}for(const [key,node] of this.children){node.owner=this;if(/form\]$/.test(key)){node.isForm=true;node.elements=this.fields;}}}if(value.includes('<option')){this.options=[...value.matchAll(/<option value="([^"]*)"/g)].map(match=>({value:match[1]}));this.value=this.options[0]?.value||'';}}
  get innerHTML(){return this._html;}
  querySelector(selector){if(this.isHost)return this.children.get(selector)||null;if(this.isForm){if(selector==='[type=submit]')return this.button??=new Element();const name=selector.match(/^\[name=([^\]]+)\]$/)?.[1];if(name)return this.owner.children.get(selector)||this.elements[name];}return this.children.get(selector)||null;}
  querySelectorAll(){return[];}
  addEventListener(name,fn){this.events[name]=fn;}
  removeEventListener(name){delete this.events[name];}
  setAttribute(name,value){if(name.startsWith('data-'))this.dataset[name.slice(5).replace(/-([a-z])/g,(_,v)=>v.toUpperCase())]=value;}
  matches(selector){return selector==='[data-page-error]'&&this.dataset.pageError!==undefined;}
  closest(){return null;}
  prepend(node){this.children.set('[data-page-error]',node);}
  replaceChildren(){this.innerHTML='';}
  reset(){for(const node of Object.values(this.elements)){node.value='';node.checked=false;}}
  reportValidity(){return true;}
}
const host=new Element();host.isHost=true;host.fields=Object.fromEntries(['star','chinext','risk_warning'].map(name=>[name,new Element()]));
globalThis.document={getElementById(){return null;},createElement(){return new Element();}};
globalThis.history={replaceState(...args){urls.push(args[2]);}};
globalThis.FormData=class{constructor(form){this.form=form;}get(name){return this.form.querySelector(`[name=${name}]`)?.value||'';}};
const controller=new AbortController(),flush=()=>new Promise(resolve=>setTimeout(resolve,0));
function account(id,asOf='2026-10-26'){return{account_id:id,name:id,latest_snapshot_id:`snapshot-${id}`,is_fixture:false,snapshot:{snapshot_id:`snapshot-${id}`,account_id:id,as_of:asOf,cash:0,positions:[],permissions:{}}};}
const accounts=[account('A'),account('B')],source={source_id:'source-one',source_kind:'fusion_scan_v1',run_id:'source-one',as_of:'2026-10-26',status:'completed'};
api.tradingAccounts=async()=>({accounts});api.tradingAccount=async id=>accounts.find(row=>row.account_id===id);
api.fusionRuns=async()=>({reports:[source]});api.tradingSelections=async()=>({selections:[source]});api.tradingPlans=async()=>({plans:[]});
api.planPreflight=async body=>({schema:'plan_preflight_v1',status:'ready',issues:[],dates:{snapshot_as_of:'2026-10-26',signal_date:'2026-10-26',execution_date:'2026-10-27'},identity:{...body,source_id:body.source_id||body.fusion_id,source_sha256:'sha-one'},next_actions:[]});
api.submit=async(kind,params)=>{submissions.push({kind,params});return{id:'test-job',kind,status:'queued'};};
const params=new URLSearchParams({account_id:'A',source_id:'source-one',fusion_id:'source-one'});
const change=(node,value)=>{node.value=value;return node.events.change({target:node});};
const submit=()=>host.querySelector('[data-plan-form]').events.submit({preventDefault(){},currentTarget:host.querySelector('[data-plan-form]')});
"""


def test_failed_plan_keeps_account_and_forms_available_for_correction():
    run_module(SETUP + r"""
api.submit=async()=>({id:'saved-failed-job',kind:'trade_plan',status:'failed',error:'ValueError: Account snapshot contains information after the signal date'});
const cleanup=await mountTrading(host,{signal:controller.signal,params});
await submit();await flush();await flush();
assert(host.querySelector('[data-trading-account]'),'task error removed the account selector');
assert(host.querySelector('[data-snapshot-form]'),'task error removed the snapshot form');
assert(host.querySelector('[data-plan-form]'),'task error removed the plan form');
assert(host.querySelector('[data-plan-error]').innerHTML.includes('Account snapshot contains information after the signal date'));
cleanup();controller.abort();
""")


def test_incompatible_dates_block_submission_without_a_failed_job():
    run_module(SETUP + r"""
source.as_of='2026-10-22';
api.planPreflight=async body=>({status:'blocked',identity:body,dates:{snapshot_as_of:'2026-10-26',signal_date:'2026-10-22',execution_date:'2026-10-23'},issues:[{code:'snapshot_after_signal',message:'账户快照晚于信号日'}],next_actions:['选择同日信号']});
const cleanup=await mountTrading(host,{signal:controller.signal,params});
await submit();await flush();
assert.equal(submissions.length,0,'incompatible dates reached job submission');
assert.equal(host.querySelector('[data-plan-form]').querySelector('[type=submit]').disabled,true);
assert(host.querySelector('[data-plan-preflight]').innerHTML.includes('2026-10-22'));
cleanup();controller.abort();
""")


def test_late_account_response_cannot_replace_the_newer_selection():
    run_module(SETUP + r"""
const cleanup=await mountTrading(host,{signal:controller.signal,params:new URLSearchParams()});
let resolveA,resolveB;api.tradingAccount=id=>new Promise(resolve=>{if(id==='A')resolveA=resolve;else resolveB=resolve;});
const select=host.querySelector('[data-trading-account]');
const a=change(select,'A'),b=change(select,'B');resolveB(accounts[1]);await b;resolveA(accounts[0]);await a;
assert(host.querySelector('[data-current-account]').innerHTML.includes('>B /'));
assert(!host.querySelector('[data-current-account]').innerHTML.includes('>A /'));
await change(host.querySelector('[name=source_id]')||host.querySelector('[name=fusion_id]'),'source-one');
await submit();assert.equal(submissions.at(-1)?.params.account_id,'B');
cleanup();controller.abort();
""")


def test_show_error_without_a_slot_preserves_the_existing_component():
    run_module(SETUP + r"""
host.innerHTML='<form data-essential-form></form>';
showError(host,new Error('<unsafe>'));
assert(host.querySelector('[data-essential-form]'));
assert(host.querySelector('[data-page-error]').innerHTML.includes('&lt;unsafe&gt;'));
showError(host,new Error('second error'));
assert(host.querySelector('[data-essential-form]'));
assert(host.querySelector('[data-page-error]').innerHTML.includes('second error'));
""")


def test_plan_report_labels_factor_and_legacy_fusion_sources_without_inventing_one():
    run_module(r"""
import assert from 'node:assert/strict';
import {planReport} from './core/trading.js';
import {kindLabel} from './core/ui.js';
const factor=planReport({source_kind:'model_scan_v1',source_id:'scan-<one>',orders:[],holds:[]});
assert(factor.includes('因子选股'));assert(factor.includes('scan-&lt;one&gt;'));
assert(!factor.includes('融合/对照选股'));
const legacy=planReport({fusion_id:'fusion-old',orders:[],holds:[]});
assert(legacy.includes('融合/对照选股'));assert(legacy.includes('fusion-old'));
assert.equal(kindLabel('scan_explanation'),'指标解释附件');
""")


def test_clearing_account_invalidates_an_inflight_read():
    run_module(SETUP + r"""
const cleanup=await mountTrading(host,{signal:controller.signal,params:new URLSearchParams()});
let resolve;api.tradingAccount=()=>new Promise(done=>{resolve=done;});
const select=host.querySelector('[data-trading-account]'),pending=change(select,'A');
await change(select,'');resolve(accounts[0]);await pending;
assert(host.querySelector('[data-current-account]').innerHTML.includes('尚未选择'));
assert.equal(host.querySelector('[data-save-snapshot]').disabled,true);
await submit();assert.equal(submissions.length,0);
cleanup();controller.abort();
""")


def test_stale_preflight_cannot_enable_a_different_source_or_overwrite_its_error():
    run_module(SETUP + r"""
const second={...source,source_id:'source-two',run_id:'source-two',as_of:'2026-10-22'};
api.tradingSelections=async()=>({selections:[source,second]});
const cleanup=await mountTrading(host,{signal:controller.signal,params});
const original=api.planPreflight;let resolveFirst;
api.planPreflight=body=>body.source_id==='source-one'?new Promise(resolve=>{resolveFirst=()=>original(body).then(resolve);}):Promise.resolve({status:'blocked',issues:[{message:'NEW BLOCKED SOURCE'}],dates:{signal_date:'2026-10-22'}});
const select=host.querySelector('[name=source_id]'),old=change(select,'source-one');
await change(select,'source-two');resolveFirst();await old;
assert(host.querySelector('[data-plan-preflight]').innerHTML.includes('NEW BLOCKED SOURCE'));
assert.equal(host.querySelector('[data-plan-form]').querySelector('[type=submit]').disabled,true);
await submit();assert.equal(submissions.length,0);
cleanup();controller.abort();
""")


def test_stale_preflight_failure_and_unmount_cannot_replace_newer_content():
    run_module(SETUP + r"""
const cleanup=await mountTrading(host,{signal:controller.signal,params});
let rejectFirst;const ready=api.planPreflight;
api.planPreflight=()=>new Promise((resolve,reject)=>{rejectFirst=reject;});
const select=host.querySelector('[name=source_id]'),old=change(select,'source-one');
api.planPreflight=ready;await change(select,'source-one');
rejectFirst(new Error('STALE ERROR'));await old;
assert(!host.querySelector('[data-plan-error]').innerHTML.includes('STALE ERROR'));
assert.equal(host.querySelector('[data-plan-form]').querySelector('[type=submit]').disabled,false);
let finish;api.tradingAccount=()=>new Promise(resolve=>{finish=resolve;});
const accountRead=change(host.querySelector('[data-trading-account]'),'B');cleanup();
const content=host.querySelector('[data-current-account]').innerHTML;finish(accounts[1]);await accountRead;
assert.equal(host.querySelector('[data-current-account]').innerHTML,content);controller.abort();
""")


def test_ready_response_requires_matching_frozen_identity_and_submission_keeps_it():
    run_module(SETUP + r"""
const cleanup=await mountTrading(host,{signal:controller.signal,params});
const ready=api.planPreflight;
api.planPreflight=async body=>({...await ready(body),identity:{...body,account_id:'OTHER',source_sha256:'sha-one'}});
await submit();assert.equal(submissions.length,0);
assert(host.querySelector('[data-plan-error]').innerHTML.includes('身份不一致'));
api.planPreflight=ready;await change(host.querySelector('[name=source_id]'),'source-one');
await submit();assert.deepEqual(submissions.at(-1),{kind:'trade_plan',params:{account_id:'A',snapshot_id:'snapshot-A',source_id:'source-one',source_kind:'fusion_scan_v1',source_sha256:'sha-one'}});
assert(urls.at(-1).includes('plan_job=test-job'));assert(urls.at(-1).includes('source_id=source-one'));
assert(!urls.at(-1).includes('cash'));assert(!urls.at(-1).includes('positions'));
cleanup();controller.abort();
""")


def test_restore_failed_job_is_read_only_and_keeps_form_edits():
    run_module(SETUP + r"""
let reads=0;api.job=async()=>{reads++;return{id:'saved-job',kind:'trade_plan',status:'failed',params:{account_id:'A',snapshot_id:'snapshot-A',fusion_id:'source-one'},error:'SAVED FAILURE'};};
params.set('plan_job','saved-job');const cleanup=await mountTrading(host,{signal:controller.signal,params});await flush();await flush();
assert.equal(reads,1);assert.equal(submissions.length,0);
assert(host.querySelector('[data-plan-error]').innerHTML.includes('SAVED FAILURE'));
assert(host.querySelector('[data-snapshot-form]'));
api.submit=async()=>{host.querySelector('[name=cash]').value='123';return{id:'failed-job',kind:'trade_plan',status:'failed',error:'SECOND FAILURE'};};
await submit();await flush();await flush();assert.equal(host.querySelector('[name=cash]').value,'123');
cleanup();controller.abort();
""")


def test_fast_success_restores_report_url_without_overwriting_it_with_job_url():
    run_module(SETUP + r"""
api.submit=async()=>({id:'fast',kind:'trade_plan',status:'succeeded',result:{plan_id:'plan-saved',account_id:'A',orders:[],holds:[]}});
const cleanup=await mountTrading(host,{signal:controller.signal,params});await submit();await flush();
assert(host.querySelector('[data-plan-result]').innerHTML.includes('plan-saved'));
assert(urls.at(-1).includes('plan_id=plan-saved'));assert(!urls.at(-1).includes('plan_job='));
cleanup();controller.abort();
""")


def test_old_plan_read_cannot_replace_a_newer_plan_or_a_changed_account():
    run_module(SETUP + r"""
const cleanup=await mountTrading(host,{signal:controller.signal,params});
let resolveA,resolveB;api.tradingPlan=id=>new Promise(resolve=>{if(id==='plan-A')resolveA=resolve;else resolveB=resolve;});
const open=id=>host.querySelector('[data-trading-plans]').events.click({target:{closest(){return{dataset:{openPlan:id},disabled:false,textContent:'open'};}}});
const a=open('plan-A'),b=open('plan-B');resolveB({plan_id:'plan-B',account_id:'A',orders:[],holds:[]});await b;
resolveA({plan_id:'plan-A',account_id:'A',orders:[],holds:[]});await a;
assert(host.querySelector('[data-plan-result]').innerHTML.includes('plan-B'));
assert(!host.querySelector('[data-plan-result]').innerHTML.includes('plan-A'));
const old=open('plan-A');await change(host.querySelector('[data-trading-account]'),'B');
resolveA({plan_id:'plan-A',account_id:'A',orders:[],holds:[]});await old;
assert.equal(host.querySelector('[data-plan-result]').innerHTML,'');
cleanup();controller.abort();
""")


def test_late_import_template_cannot_overwrite_another_accounts_form():
    run_module(SETUP + r"""
const cleanup=await mountTrading(host,{signal:controller.signal,params});
let finish;api.tradingTemplate=()=>new Promise(resolve=>{finish=resolve;});
const button=host.querySelector('[data-load-template]'),pending=button.events.click({currentTarget:button});
await change(host.querySelector('[data-trading-account]'),'B');
const value=host.querySelector('[name=snapshot_json]').value;
finish({snapshot:{as_of:'2000-01-01',cash:999,positions:[]}});await pending;
assert.equal(host.querySelector('[name=snapshot_json]').value,value);
cleanup();controller.abort();
""")


def test_old_account_save_failure_cannot_replace_the_new_accounts_error_slot():
    run_module(SETUP + r"""
const cleanup=await mountTrading(host,{signal:controller.signal,params});
let fail;api.saveTradingSnapshot=()=>new Promise((resolve,reject)=>{fail=reject;});
const button=host.querySelector('[data-save-snapshot]'),pending=button.events.click({currentTarget:button});
await change(host.querySelector('[data-trading-account]'),'B');fail(new Error('OLD ACCOUNT SAVE ERROR'));await pending;
assert(!host.querySelector('[data-account-error]').innerHTML.includes('OLD ACCOUNT SAVE ERROR'));
cleanup();controller.abort();
""")


PAGE_SETUP = SETUP + r"""
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const calls={trading:0,watchlist:0,reports:0,accounts:0,models:0,disposed:0};
globalThis.tradingMount=async()=>{calls.trading++;return()=>calls.disposed++;};
globalThis.defaultMount=async()=>{calls.models++;};
api.watchlist=async()=>{calls.watchlist++;return{symbols:[]};};
api.reports=async()=>{calls.reports++;return{reports:[]};};
api.accounts=async()=>{calls.accounts++;return{accounts:[]};};
const pageHost=new Element(),pageNodes=new Map(),tabs=['stocks','accounts','trading'].map(tab=>{const node=new Element();node.dataset.tab=tab;return node;});
pageHost.querySelector=selector=>{if(!pageNodes.has(selector)){const node=new Element();node.after=()=>{};node.insertAdjacentHTML=()=>{};node.closest=()=>new Element();pageNodes.set(selector,node);}return pageNodes.get(selector);};
pageHost.querySelectorAll=selector=>selector==='[data-tab]'?tabs:[];pageHost.insertAdjacentHTML=()=>{};
const file=pathToFileURL(process.cwd()+'/pages/watchlist.js');
const sourceText=fs.readFileSync(file,'utf8').replace("import {mountTrading} from '../core/trading.js';",'const mountTrading=(...args)=>globalThis.tradingMount(...args);').replace("import {mountDefaultModel} from '../core/models.js';",'const mountDefaultModel=(...args)=>globalThis.defaultMount(...args);').replace(/from '([^']+)'/g,(_,name)=>`from '${new URL(name,file).href}'`);
const watchPage=await import('data:text/javascript;base64,'+Buffer.from(sourceText).toString('base64'));
const tabClick=name=>tabs.find(tab=>tab.dataset.tab===name).events.click();
"""


def test_watchlist_tabs_load_only_their_own_data_and_persist_the_tab_url():
    run_module(PAGE_SETUP + r"""
const state=new URLSearchParams(),cleanup=await watchPage.mount(pageHost,{signal:controller.signal,params:state});
assert.deepEqual(calls,{trading:0,watchlist:1,reports:0,accounts:0,models:1,disposed:0});
await tabClick('trading');assert.equal(calls.trading,1);assert.equal(calls.reports,0);assert(urls.at(-1).includes('tab=trading'));
await tabClick('accounts');assert.equal(calls.reports,1);assert.equal(calls.accounts,1);
await tabClick('trading');assert.equal(calls.trading,1);
cleanup();assert.equal(calls.disposed,1);controller.abort();
""")


def test_account_identity_deep_link_opens_trading_without_loading_other_tabs():
    run_module(PAGE_SETUP + r"""
const cleanup=await watchPage.mount(pageHost,{signal:controller.signal,params:new URLSearchParams({account_id:'A',fusion_id:'saved-source'})});
assert.deepEqual(calls,{trading:1,watchlist:0,reports:0,accounts:0,models:0,disposed:0});
assert.equal(pageHost.querySelector('[data-watch-trading]').hidden,false);
cleanup();controller.abort();
""")


def test_explicit_fixture_account_url_restores_it_with_a_visible_fixture_warning():
    run_module(SETUP + r"""
accounts[0].is_fixture=true;
const cleanup=await mountTrading(host,{signal:controller.signal,params});
assert.equal(host.querySelector('[data-show-fixtures]').checked,true);
assert.equal(host.querySelector('[data-trading-account]').value,'A');
assert(host.querySelector('[data-current-account]').innerHTML.includes('合成验收账户'));
assert.equal(submissions.length,0);
cleanup();controller.abort();
""")


def test_default_account_list_keeps_fixtures_hidden_without_an_explicit_identity():
    run_module(SETUP + r"""
accounts[0].is_fixture=true;
const cleanup=await mountTrading(host,{signal:controller.signal,params:new URLSearchParams()});
assert.equal(host.querySelector('[data-show-fixtures]').checked,false);
assert(!host.querySelector('[data-trading-account]').options.some(row=>row.value==='A'));
assert.equal(submissions.length,0);
cleanup();controller.abort();
""")


def test_late_lazy_mount_is_disposed_after_page_exit():
    run_module(PAGE_SETUP + r"""
const cleanup=await watchPage.mount(pageHost,{signal:controller.signal,params:new URLSearchParams()});
let finish;globalThis.tradingMount=()=>new Promise(resolve=>{finish=()=>resolve(()=>calls.disposed++);});
const pending=tabClick('trading');cleanup();finish();await pending;
assert.equal(calls.disposed,1);controller.abort();
""")

