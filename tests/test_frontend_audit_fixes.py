import json
import shutil
import subprocess
from pathlib import Path

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def run_module(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for frontend behavior checks")
    result = subprocess.run(
        [node, "--input-type=module", "-e", source],
        cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_saved_analysis_restores_form_actions_and_saved_identity():
    run_module(r"""
import assert from 'node:assert/strict';
import {applyAnalysisResult} from './pages/analysis.js';
const fields={symbol:{value:''},start:{value:'2026-01-01'},end:{value:'2026-10-26'}};
const form={querySelector:selector=>fields[selector.match(/name=(\w+)/)[1]]};
globalThis.FormData=class{get(key){return fields[key].value;}};
const elements=new Map();
const host={querySelector(selector){if(selector==='#analysis-form')return form;if(!elements.has(selector))elements.set(selector,{disabled:true,innerHTML:'waiting',replaceChildren(){this.innerHTML='';}});return elements.get(selector);}};
const saved={symbol:'002436.SZ',start:'2022-07-05',end:'2026-10-23',as_of:'2026-10-24',data_version:{generation:'frozen-1'},price_basis:'unadjusted',analysis_method:'structure',data_range:{start:'2022-07-05',end:'2026-10-23',bars:920},czsc_version:'native-verified',warnings:['<unsafe>']};
applyAnalysisResult(host,saved);
assert.equal(fields.symbol.value,'002436.SZ');assert.equal(fields.start.value,'2022-07-05');assert.equal(fields.end.value,'2026-10-23');
assert.equal(host.querySelector('[data-add-watch]').disabled,false);assert.equal(host.querySelector('[data-features]').disabled,false);
const url=new URL(host.querySelector('[data-open-backtest]').href,'http://local/');
const query=new URLSearchParams(url.hash.split('?')[1]);assert.equal(query.get('symbols'),'002436.SZ');assert.equal(query.get('start'),'2022-07-05');assert.equal(query.get('end'),'2026-10-23');
assert(host.querySelector('[data-analysis-identity]').innerHTML.includes('frozen-1'));assert(host.querySelector('[data-analysis-identity]').innerHTML.includes('native-verified'));
assert(host.querySelector('[data-analysis-warnings]').innerHTML.includes('&lt;unsafe&gt;'));assert.equal(host.querySelector('[data-feature-output]').innerHTML,'');
applyAnalysisResult(host,{symbol:'600519.SH',data_range:{start:'2020-01-02',end:'2026-10-22'}});
assert.equal(fields.symbol.value,'600519.SH');assert.equal(fields.start.value,'2020-01-02');assert.equal(fields.end.value,'2026-10-22');
""")
    source = (FRONTEND / "pages/analysis.js").read_text(encoding="utf-8")
    assert source.count("showResult(response)") == 3  # declaration, new result, reopened report


def test_replay_preview_distinguishes_availability_from_new_qualification():
    run_module(r"""
import assert from 'node:assert/strict';
import {backtestPreviewHtml} from './pages/backtest.js';
const replay={status:'ready',requested_universe_scope:'explicit_symbols',effective_universe_scope:'explicit_symbols',evaluation_universe_scope:'release',eligibility_evaluated:false,counts:{requested:2,eligible:1,excluded:1},per_symbol:[{symbol:'002436.SZ',status:'available'},{symbol:'<unsafe>',status:'unavailable',reason:'not_in_saved_evaluation_data'}]};
const html=backtestPreviewHtml(replay,{mode:'watchlist_replay'});
assert(html.includes('请求股票池'));assert(html.includes('实际交易股票池'));assert(html.includes('历史评价序列来源'));
assert(html.includes('显式股票池'));assert(html.includes('默认方法资格股票池'));
assert(html.includes('历史序列可用 1 / 不可用 1'));assert(html.includes('未重新核验当前股票池资格'));
assert(html.includes('历史序列可用性'));assert(!html.includes('60日成交额中位'));assert(!html.includes('资格上限'));assert(!html.includes('合格 1'));assert(!html.includes('<unsafe>'));
const frozen=backtestPreviewHtml({...replay,eligibility_evaluated:true,per_symbol:[{symbol:'002436.SZ',status:'eligible',coverage252:1,coverage60:.95}],cap:1000},{mode:'frozen'});
assert(frozen.includes('合格 1 / 排除 1'));assert(frozen.includes('资格状态'));assert(frozen.includes('60日成交额中位'));assert(frozen.includes('95%'));assert(!frozen.includes('未重新核验'));
const legacy=backtestPreviewHtml({universe_scope:'explicit_symbols',counts:{requested:1,eligible:1},per_symbol:[{symbol:'002436.SZ',status:'available'}]},{mode:'watchlist_replay',universe_scope:'explicit_symbols'});
assert(legacy.includes('历史序列可用 1'));assert(legacy.includes('未重新核验'));
const pending=backtestPreviewHtml({mode:'frozen',status:'partial',eligibility_evaluated:false,counts:{requested:5},blockers:['universe_build_required']},{mode:'frozen'});
assert(pending.includes('资格待核验'));assert(!pending.includes('历史序列可用性'));assert(!pending.includes('合格 0'));
""")


def test_daily_preview_keeps_backend_target_and_health_is_lightweight():
    run_module(r"""
import assert from 'node:assert/strict';
import {api} from './core/api.js';
const calls=[];
globalThis.fetch=async(url,options)=>{calls.push({url,options});return{ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.health();await api.pipelinePreview({packs:'full',refit:'auto'});
assert.equal(calls[0].url,'/api/v1/health');assert.equal(calls[1].url,'/api/v1/pipeline/preview?packs=full&refit=auto');
assert(calls.every(call=>call.options.method===undefined));
""")
    overview = (FRONTEND / "pages/overview.js").read_text(encoding="utf-8")
    assert "api.pipelinePreview({packs:'full',refit:'auto'},signal)" in overview
    assert "end:today()" not in overview
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    connection = app.split("async function connection(){", 1)[1].split("function clock()", 1)[0]
    assert "api.health()" in connection
    assert "api.overview()" not in connection


def test_task_search_and_pagination_query_history_beyond_first_page():
    run_module(r"""
import assert from 'node:assert/strict';
import {mount} from './pages/tasks.js';
import {api} from './core/api.js';
const nodes=new Map();
function node(){return {value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,events:{},insertAdjacentHTML(){},addEventListener(name,fn){this.events[name]=fn;},querySelector(){return node();},querySelectorAll(){return [];}};}
const host={...node(),querySelector(selector){if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);}};
const calls=[];
api.jobs=async(signal,values)=>{calls.push({type:'jobs',...values});return{jobs:[{id:values.q?values.q+'-job':'job-'+values.offset,kind:'pipeline',status:'succeeded',result:{params:{dry_run:true}}}],total:values.q?1:121,offset:values.offset,has_more:!values.q&&values.offset<100};};
api.reports=async(signal,values)=>{calls.push({type:'reports',...values});return{reports:[{run_id:values.q?'oldest-report':'report-'+values.offset,kind:'preview',status:'completed',status_label:'只读预检（历史记录）'}],total:values.q?1:121,offset:values.offset,has_more:!values.q&&values.offset<100};};
api.quarantinePreview=async()=>({items:[]});
const cleanup=await mount(host,{signal:new AbortController().signal,params:new URLSearchParams()});
assert(calls.every(call=>call.offset===0&&call.limit===50));assert.equal(host.querySelector('[data-jobs-next]').disabled,false);
assert(host.querySelector('[data-jobs-table]').innerHTML.includes('流水线预检'));assert(host.querySelector('[data-jobs-table]').innerHTML.includes('只读预检（历史记录）'));
host.querySelector('[data-jobs-next]').events.click();await new Promise(r=>setTimeout(r,0));
assert.equal(calls.filter(c=>c.type==='jobs').at(-1).offset,50);assert(host.querySelector('[data-jobs-table]').innerHTML.includes('job-50'));
host.querySelector('[data-job-search]').value='oldest';host.querySelector('[data-job-search]').events.input();await new Promise(r=>setTimeout(r,300));
const found=calls.filter(c=>c.type==='jobs').at(-1);assert.equal(found.offset,0);assert.equal(found.q,'oldest');assert(host.querySelector('[data-jobs-table]').innerHTML.includes('oldest-job'));assert.equal(host.querySelector('[data-jobs-next]').disabled,true);
host.querySelector('[data-report-search]').value='oldest';host.querySelector('[data-report-search]').events.input();await new Promise(r=>setTimeout(r,300));
assert.equal(calls.filter(c=>c.type==='reports').at(-1).q,'oldest');assert(host.querySelector('[data-reports-table]').innerHTML.includes('oldest-report'));assert(host.querySelector('[data-reports-table]').innerHTML.includes('只读预检（历史记录）'));assert(host.querySelector('[data-reports-table]').innerHTML.includes('流水线预检'));
const normalJobs=api.jobs;let releaseOld;
api.jobs=async(signal,values)=>values.q==='slow'?new Promise(resolve=>{releaseOld=resolve;}):normalJobs(signal,values);
host.querySelector('[data-job-search]').value='slow';host.querySelector('[data-job-search]').events.input();await new Promise(r=>setTimeout(r,300));
host.querySelector('[data-job-search]').value='newest';host.querySelector('[data-job-search]').events.input();await new Promise(r=>setTimeout(r,300));
releaseOld({jobs:[{id:'stale-job',kind:'backtest',status:'failed'}],total:1});await new Promise(r=>setTimeout(r,0));
assert(host.querySelector('[data-jobs-table]').innerHTML.includes('newest-job'));assert(!host.querySelector('[data-jobs-table]').innerHTML.includes('stale-job'));
cleanup();
""")


def test_paginated_api_preserves_legacy_call_signature():
    run_module(r"""
import assert from 'node:assert/strict';
import {api} from './core/api.js';
const urls=[];
globalThis.fetch=async(url,options)=>{urls.push(url);return{ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.jobs();await api.reports();await api.jobs(undefined,{q:'oldest',status:'failed',kind:'backtest',offset:100,limit:50});await api.reports(undefined,{q:'oldest',offset:100,limit:50});
assert.deepEqual(urls,['/api/v1/jobs','/api/v1/reports','/api/v1/jobs?q=oldest&status=failed&kind=backtest&offset=100&limit=50','/api/v1/reports?q=oldest&offset=100&limit=50']);
""")


def test_browser_smoke_reports_missing_runtime_as_failure(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser smoke entrypoint checks")
    script = FRONTEND.parent / "scripts/validate_browser.mjs"
    help_result = subprocess.run([node, str(script), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0
    assert "--url" in help_result.stdout
    result = subprocess.run(
        [node, str(script), "--playwright", str(tmp_path / "missing-playwright"), "--out", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["errors"][0]["check"] == "setup"
    assert "missing-playwright" in summary["errors"][0]["error"]
    assert summary["checks"] == []


def test_historical_dry_run_results_keep_the_preview_label():
    run_module(r"""
import assert from 'node:assert/strict';
import {isPipelinePreview,reportStatusLabel,reportList} from './core/reports.js';
for(const record of [{kind:'preview'},{params:{dry_run:true}},{config:{dry_run:true}}]){
assert.equal(isPipelinePreview(record),true);assert.equal(reportStatusLabel(record),'只读预检（历史记录）');
const html=reportList([{...record,run_id:'historical-preview',status:'completed'}]);assert(html.includes('流水线预检'));assert(html.includes('只读预检（历史记录）'));assert(!html.includes('已完成'));
}
assert.equal(isPipelinePreview({kind:'pipeline',params:{dry_run:false}}),false);
assert.equal(reportStatusLabel({kind:'preview',status_label:'只读预检'}),'只读预检');
assert.equal(reportStatusLabel({kind:'backtest'}),undefined);
""")


def test_old_backend_lists_fall_back_to_explicit_recent_window_search():
    run_module(r"""
import assert from 'node:assert/strict';
import {mount} from './pages/tasks.js';
import {api} from './core/api.js';
const nodes=new Map();
function node(){return {value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,events:{},insertAdjacentHTML(){},addEventListener(name,fn){this.events[name]=fn;},querySelector(){return node();},querySelectorAll(){return [];}};}
const host={...node(),querySelector(selector){if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);}};
host.querySelector('[data-kind-filter]').value='backtest';host.querySelector('[data-status-filter]').value='failed';host.querySelector('[data-report-search]').value='oldest';
api.jobs=async()=>({jobs:[{id:'job-match',kind:'backtest',status:'failed'},{id:'job-success',kind:'backtest',status:'succeeded'},{id:'job-other',kind:'scan',status:'failed'}]});
api.reports=async()=>({reports:[{run_id:'oldest-report',kind:'analysis'},{run_id:'recent-report',kind:'scan'}]});api.quarantinePreview=async()=>({items:[]});
const cleanup=await mount(host,{signal:new AbortController().signal,params:new URLSearchParams()});
assert(host.querySelector('[data-jobs-table]').innerHTML.includes('job-match'));assert(!host.querySelector('[data-jobs-table]').innerHTML.includes('job-success'));assert(!host.querySelector('[data-jobs-table]').innerHTML.includes('job-other'));
assert(host.querySelector('[data-jobs-page]').textContent.includes('当前仅最近 3 条'));assert(host.querySelector('[data-jobs-page]').textContent.includes('重启后生效'));assert.equal(host.querySelector('[data-jobs-next]').disabled,true);
assert(host.querySelector('[data-reports-table]').innerHTML.includes('oldest-report'));assert(!host.querySelector('[data-reports-table]').innerHTML.includes('recent-report'));assert(host.querySelector('[data-reports-page]').textContent.includes('当前仅最近 2 条'));
host.querySelector('[data-job-search]').value='not-in-recent-window';host.querySelector('[data-job-search]').events.input();await new Promise(r=>setTimeout(r,300));assert(host.querySelector('[data-jobs-page]').textContent.includes('当前窗口匹配 0 条'));assert(host.querySelector('[data-jobs-summary]').textContent.includes('仅搜索最近 3 条'));
cleanup();
""")
