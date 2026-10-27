from test_frontend_contract import run_module


def test_pipeline_progress_shows_activity_without_inventing_completion():
    run_module(r"""
import assert from 'node:assert/strict';
import {progressSummary,jobSummary} from './core/jobs.js';
const job={id:'lake-live',kind:'pipeline',status:'running',progress:{stage:'S1',phase:'cne_data_update',message:'新浪备用源：已请求 5051 次',elapsed_seconds:5330,idle_seconds:10,warning:'主源不可用 <script>alert(1)</script>'}};
const html=progressSummary(job);
assert(html.includes('S1 · 新浪备用源：已请求 5051 次'),html);
assert(html.includes('已用 1时28分50秒'),html);
assert(html.includes('主源不可用 &lt;script&gt;alert(1)&lt;/script&gt;'),html);
assert(!html.includes('<script>'),html);
assert(!html.includes('%'),html);
assert(!html.includes('cne_data_update'),html);
assert(jobSummary(job).includes(html));
assert(!jobSummary(job).includes('width:'),jobSummary(job));
""")


def test_batch_percent_is_labeled_as_current_step_and_old_jobs_still_render():
    run_module(r"""
import assert from 'node:assert/strict';
import {progressSummary,jobSummary} from './core/jobs.js';
const batch={id:'lake-batch',kind:'pipeline',status:'running',progress:{stage:'S1',message:'日线批次',current:53,total:53,progress_scope:'current_stage',elapsed_seconds:0,idle_seconds:65}};
const html=progressSummary(batch);
assert(html.includes('当前步骤 100%'),html);
assert(html.includes('最近进展 1分5秒前'),html);
assert(jobSummary(batch).includes('width:100%'));
assert(progressSummary({id:'old',progress_detail:{stage:'history'}}).includes('history'));
assert(progressSummary({id:'error',error:'<failed>'}).includes('&lt;failed&gt;'));
assert(progressSummary({id:'empty'}).includes('empty'));
assert(!progressSummary({id:'invalid',progress:{elapsed_seconds:null,idle_seconds:Infinity}}).includes('已用'));
""")


def test_task_list_and_detail_render_the_same_readable_pipeline_progress():
    run_module(r"""
import assert from 'node:assert/strict';
import {mount} from './pages/tasks.js';
import {api} from './core/api.js';
import {progressSummary} from './core/jobs.js';
const job={id:'lake-test',kind:'pipeline',status:'running',created_at:'2026-10-26T08:52:52Z',progress:{stage:'S1',message:'新浪正在补采',warning:'连续 5 分钟未见进展'}};
const nodes=new Map();
function element(){return{innerHTML:'',textContent:'',value:'',hidden:false,disabled:false,dataset:{},events:{},addEventListener(name,fn){this.events[name]=fn;},insertAdjacentHTML(){},querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);},querySelectorAll(){return[];}};}
const host=element(),controller=new AbortController();
api.jobs=async()=>({jobs:[job],total:1,has_more:false});api.reports=async()=>({reports:[],total:0,has_more:false});api.quarantinePreview=async()=>({items:[]});api.job=async()=>job;
const cleanup=await mount(host,{signal:controller.signal,params:new URLSearchParams({job:job.id})});
const expected=progressSummary(job);
assert(nodes.get('[data-jobs-table]').innerHTML.includes(expected));
assert(nodes.get('[data-job-detail-content]').innerHTML.includes(expected));
assert(nodes.get('[data-job-detail-content]').innerHTML.includes('实际参数与执行进度'));
cleanup();controller.abort();
""")
