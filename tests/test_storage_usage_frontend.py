from test_frontend_contract import run_module


def test_storage_rendering_preserves_unknown_units_scope_and_last_good_semantics():
    run_module(r"""
import assert from 'node:assert/strict';
import {storageBytes,publishedDatasetBytes,storageUsageHtml} from './core/storage-usage.js';
assert.equal(storageBytes(null),'未知');assert.equal(storageBytes(undefined),'未知');assert.equal(storageBytes(0),'0 B');
assert.equal(storageBytes(1024),'1 KiB');assert.equal(storageBytes(Infinity),'未知');
const snapshot={scan_id:'good',status:'completed',finished_at:'2026-10-26T00:00:00Z',included_roots:['var/lake'],excluded_roots:['var/releases'],totals:{file_count:3,logical_path_bytes:24579,allocated_bytes:null,known_allocated_bytes:8192,unique_file_count:2},datasets:[{dataset:'daily_bars',logical_path_bytes:24579,current_published_logical_bytes:8193}],categories:[{label:'<unsafe>',file_count:3,logical_path_bytes:24579,exclusive_allocated_bytes:null,shared_allocated_reference_bytes:8192}],volumes:[{volume:'D:',free_bytes:10000,total_bytes:20000}]};
assert.equal(publishedDatasetBytes(snapshot,'daily_bars'),8193);assert.equal(publishedDatasetBytes({...snapshot,status:'partial'},'daily_bars'),null);assert.equal(publishedDatasetBytes(snapshot,'missing'),null);
const html=storageUsageHtml({status:'partial',stale:true,snapshot,last_attempt:{scan_id:'partial',error_count:1,errors:[{path:'<unsafe>',reason:'permission'}]}});
for(const text of ['本次部分完成','上次完整快照','文件逻辑大小','分配空间估算','未知','不能直接相加','var/releases','整卷容量'])assert(html.includes(text),text);
assert(!html.includes('<unsafe>'));assert(html.includes('&lt;unsafe&gt;'));
const empty=storageUsageHtml({status:'not_measured',snapshot:null,limits:{max_files:500000,max_seconds:180}});assert(empty.includes('尚未统计'));assert(!empty.includes('0 B'));assert(empty.includes('500,000'));assert(empty.includes('180'));assert(empty.includes('可取消'));
""")


def test_storage_mount_only_reads_cache_until_refresh_and_keeps_newest_scope():
    run_module(r"""
import assert from 'node:assert/strict';
import {mountStorageUsage} from './core/storage-usage.js';
import {api} from './core/api.js';
const nodes=new Map();function node(){return{innerHTML:'',hidden:false,disabled:false,isConnected:true,events:{},addEventListener(name,fn){this.events[name]=fn;},removeEventListener(){},replaceChildren(){this.innerHTML='';}};}
const host={...node(),querySelector(selector){if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);}};
const calls=[];let releaseRuntime;
api.storageUsage=async values=>{calls.push({kind:'get',...values});if(values.scope==='runtime')return new Promise(resolve=>{releaseRuntime=resolve;});return{status:'not_measured',snapshot:null};};
api.refreshStorageUsage=async body=>{calls.push({kind:'refresh',...body});return{id:'usage-job',kind:'storage_usage',status:'succeeded'};};
api.submit=()=>{throw Error('generic submit must not run on page load');};
const controller=new AbortController();const stop=mountStorageUsage(host,{signal:controller.signal});
await new Promise(r=>setTimeout(r,0));assert.deepEqual(calls,[{kind:'get',scope:'lake'}]);
await nodes.get('[data-usage-refresh]').events.click();await new Promise(r=>setTimeout(r,0));
assert.equal(calls.filter(row=>row.kind==='refresh').length,1);assert.equal(calls.filter(row=>row.kind==='get').length,2);
nodes.get('[data-usage-scope]').events.change({target:{value:'runtime'}});await new Promise(r=>setTimeout(r,0));
nodes.get('[data-usage-scope]').events.change({target:{value:'lake'}});await new Promise(r=>setTimeout(r,0));
releaseRuntime({status:'failed',error:'STALE_RUNTIME_SHOULD_NOT_RENDER'});await new Promise(r=>setTimeout(r,0));
assert(!nodes.get('[data-usage-content]').innerHTML.includes('STALE_RUNTIME_SHOULD_NOT_RENDER'));
assert.equal(calls.filter(row=>row.kind==='refresh').length,1);stop();controller.abort();
""")


def test_storage_tracking_failure_keeps_cache_and_allows_reconnecting():
    run_module(r"""
import assert from 'node:assert/strict';
import {mountStorageUsage} from './core/storage-usage.js';
import {api} from './core/api.js';
const nodes=new Map();function node(){return{innerHTML:'',hidden:false,disabled:false,isConnected:true,events:{},addEventListener(name,fn){this.events[name]=fn;},removeEventListener(){},replaceChildren(){this.innerHTML='';}};}
const host={...node(),querySelector(selector){if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);}};
api.storageUsage=async()=>({status:'not_measured',snapshot:null});
api.refreshStorageUsage=async()=>({id:'still-running',kind:'storage_usage',status:'running'});
api.job=async()=>{throw new Error('connection interrupted');};
const stop=mountStorageUsage(host,{signal:new AbortController().signal});await Promise.resolve();await Promise.resolve();
const original=nodes.get('[data-usage-content]').innerHTML;
let poll;globalThis.setTimeout=fn=>{poll=fn;return 1;};globalThis.clearTimeout=()=>{};
await nodes.get('[data-usage-refresh]').events.click();assert.equal(nodes.get('[data-usage-refresh]').disabled,true);
await poll();assert.equal(nodes.get('[data-usage-refresh]').disabled,false);assert.equal(nodes.get('[data-usage-content]').innerHTML,original);
assert(nodes.get('[data-usage-job]').innerHTML.includes('任务可能仍在运行'));stop();
""")
