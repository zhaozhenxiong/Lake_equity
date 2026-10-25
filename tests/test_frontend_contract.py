import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def run_module(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for frontend behavior checks")
    result = subprocess.run(
        [node, "--input-type=module", "-e", source],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_all_local_module_imports_resolve():
    for module in FRONTEND.rglob("*.js"):
        if "vendor" in module.parts:
            continue
        for imported in re.findall(r"(?:from\s*|import\()\s*['\"](\.[^'\"]+)['\"]", module.read_text(encoding="utf-8")):
            assert (module.parent / imported).is_file(), (module, imported)


def test_public_shell_has_all_unified_pages():
    shell = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    for page in ("overview", "data", "analysis", "scan", "backtest", "watchlist", "models", "tasks"):
        assert f"id:'{page}'" in app
    assert 'type="module"' in shell
    assert 'href="#data?tab=operations"' in shell
    assert 'href="#models"' in shell
    assert "<iframe" not in shell


def test_business_table_styles_do_not_change_vendor_chart_layout():
    stylesheet = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    table_elements = r"(?:^|[\s>+~])(?:table|thead|tbody|tr|th|td)(?=[\s.#:\[>+~]|$)"
    for selector_group in re.findall(r"([^{}]+)\{", stylesheet):
        for selector in selector_group.split(","):
            if re.search(table_elements, selector.strip()):
                assert ".table-wrap " in selector or ".catalog-table " in selector, selector


def test_unified_frontend_never_calls_or_links_retired_services():
    for path in FRONTEND.rglob("*"):
        if not path.is_file() or "vendor" in path.parts or path.suffix not in {".js", ".html", ".md"}:
            continue
        source = path.read_text(encoding="utf-8")
        for retired in ("/native/", "/api/khquant", "/api/lake"):
            assert retired not in source, (path, retired)


def test_api_preserves_job_parameters_and_reports_validation_errors():
    run_module(r"""
import assert from 'node:assert/strict';
import {api, ApiError, queryString} from './core/api.js';
const calls=[];
globalThis.fetch=async (url,options)=>{calls.push({url,options});return {ok:true,status:202,headers:{get:()=> 'application/json'},json:async()=>({id:'frozen-run',status:'queued'})};};
await api.submit('backtest',{symbols:['600519.SH'],initial_cash:100000,start:'2026-01-01',end:'2026-10-21'});
assert.equal(calls[0].url,'/api/v1/jobs');
assert.deepEqual(JSON.parse(calls[0].options.body),{kind:'backtest',params:{symbols:['600519.SH'],initial_cash:100000,start:'2026-01-01',end:'2026-10-21'}});
assert.equal(calls[0].options.credentials,'same-origin');
assert.equal(queryString({symbol:'600519.SH',missing:undefined,empty:'',limit:0}),'?symbol=600519.SH&limit=0');
globalThis.fetch=async()=>({ok:false,status:422,headers:{get:()=> 'application/json'},json:async()=>({detail:[{loc:['body','params','symbols'],msg:'minimum 1 item'}]})});
await assert.rejects(api.submit('scan',{}),error=>error instanceof ApiError && error.status===422 && error.message.includes('params.symbols: minimum 1 item'));
""")


def test_missing_values_and_untrusted_content_are_not_rendered_as_success():
    run_module(r"""
import assert from 'node:assert/strict';
import {number, percent, escapeHtml, symbolsFrom, activeJob, progressValue, badge, dateTime} from './core/ui.js';
assert.equal(number(null),'—');
assert.equal(percent(undefined),'—');
assert.equal(number(0),'0');
assert.equal(percent(0),'0%');
assert.equal(escapeHtml('<img src=x onerror="alert(1)">'),'&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
assert.throws(()=>symbolsFrom('600519.SH,000001.SZ',1),/最多/);
assert.throws(()=>symbolsFrom('830799.BJ'),/沪深/);
assert.deepEqual(symbolsFrom('600519.sh，600519.SH'),['600519.SH']);
assert.equal(activeJob({status:'blocked'}),false);
assert.equal(progressValue({progress:{current:2,total:4}}),50);
assert.equal(progressValue({progress:{stage:'准备'}}),null);
assert.equal(progressValue({progress:{percent:null}}),null);
assert(badge('blocked').includes('已阻止'));
assert.equal(dateTime('2026-10-22T11:36:00+00:00'),'2026-10-22 19:36:00');
assert.equal(dateTime('2026-10-22T18:53:00+08:00'),'2026-10-22 18:53:00');
assert.equal(dateTime('2026-10-22'),'2026-10-22');
""")


def test_charts_reject_unsorted_bars_and_do_not_invent_verified_history():
    run_module(r"""
import assert from 'node:assert/strict';
import {normalizeBars,predictionSeries} from './core/charts.js';
const bar=date=>({date,open:10,high:12,low:9,close:11,volume:100});
assert.throws(()=>normalizeBars([bar('2026-10-21'),bar('2026-10-20')]),/顺序/);
assert.throws(()=>normalizeBars([{...bar('2026-10-20'),low:11}]),/OHLC/);
assert.throws(()=>normalizeBars([{...bar('2026-10-20'),open:null}]),/OHLC/);
const input={as_of:'2026-10-20',history:[bar('2026-10-20')],forecast:[{date:'2026-10-21',close:12}]};
assert.equal(predictionSeries(input).history.length,0);
assert.equal(predictionSeries({...input,history_verification:{status:'verified',input_sha256_match:true}}).history.length,1);
assert.throws(()=>predictionSeries({...input,forecast:[{date:'2026-10-20',close:12}]}),/交易日/);
""")


def test_report_renderer_keeps_independent_accounts():
    run_module(r"""
import assert from 'node:assert/strict';
import {reportAccounts} from './core/reports.js';
const values=reportAccounts({comparisons:{structure_rules:{daily:[],metrics:{total_return:0}},structure_kronos:{daily:[],metrics:{total_return:-.1}}}});
assert.equal(values.length,2);
assert.equal(values[0].key,'structure_rules');
assert.equal(values[0].metrics.total_return,0);
assert.equal(values[1].metrics.total_return,-.1);
assert.deepEqual(reportAccounts({status:'blocked'}),[]);
""")


def test_task_view_reads_terminal_results_and_stops_polling_when_disposed():
    run_module(r"""
import assert from 'node:assert/strict';
import {watchJob} from './core/jobs.js';
const listeners=new Map();
const host={isConnected:true,innerHTML:'',addEventListener:(kind,callback)=>listeners.set(kind,callback),removeEventListener:(kind,callback)=>{if(listeners.get(kind)===callback)listeners.delete(kind);}};
let completed=0;
const terminal=watchJob(host,{id:'saved',kind:'training',status:'succeeded',result:{status:'blocked'}},{onComplete:job=>{completed++;assert.equal(job.result.status,'blocked');}});
assert.equal(completed,1);
terminal.stop();
assert.equal(listeners.size,0);
let scheduled;
globalThis.setTimeout=callback=>{scheduled=callback;return 1;};
globalThis.clearTimeout=()=>{};
globalThis.fetch=()=>{throw new Error('disposed task must not poll');};
const running=watchJob(host,{id:'running',kind:'backtest',status:'running'});
running.stop();
await scheduled();
assert.equal(listeners.size,0);
""")


def test_event_panel_deduplicates_only_matching_source_event_identity():
    run_module(r"""
import assert from 'node:assert/strict';
import {evidenceRows} from './pages/analysis.js';
const source={symbol:'600519.SH',source:'wyckoff',event_id:'same-event',first_available_at:'2026-10-20T15:00:00+08:00',action:'SELL',reference_price:10};
const raw={symbol:'600519.SH',event_id:'same-event',first_available_at:'2026-10-20T15:00:00+08:00',event:'Upthrust'};
const input={symbol:'600519.SH',source_reference_points:[source],wyckoff_events:[raw,{...raw,event_id:'another-event'},{...raw,event_id:undefined}],ict_events:[{...raw,event_id:'same-event'}]};
const original=JSON.stringify(input);
const rows=evidenceRows(input);
assert.equal(rows.length,4);
const shared=rows.find(row=>row.family==='wyckoff' && row.event_id==='same-event');
assert.equal(shared.event,'Upthrust');
assert.equal(shared.action,'SELL');
assert.equal(shared.reference_price,10);
assert(rows.some(row=>row.family==='ict' && row.event_id==='same-event'));
assert.equal(JSON.stringify(input),original);
""")


def test_late_cancellation_preserves_completed_status_and_displays_backend_note():
    run_module(r"""
import assert from 'node:assert/strict';
import {cancellationNotice,jobSummary} from './core/jobs.js';
const completed={id:'late-cancel',kind:'backtest',status:'succeeded',cancel_requested:true,cancel_effective:false,cancel_note:'取消请求到达时本次计算已完成并保存；结果保留。'};
const notice=cancellationNotice(completed);
assert(notice.includes('取消未生效'));
assert(notice.includes(completed.cancel_note));
const summary=jobSummary(completed);
assert(summary.includes('已完成'));
assert(summary.includes('取消未生效'));
assert(!summary.includes('已取消'));
assert(!summary.includes('data-cancel-inline'));
assert.equal(cancellationNotice({status:'running'}),'');
assert(cancellationNotice({cancel_effective:true}).includes('取消已生效'));
assert(cancellationNotice({cancel_note:'<script>alert(1)</script>'}).includes('&lt;script&gt;'));
""")
    tasks = (FRONTEND / "pages" / "tasks.js").read_text(encoding="utf-8")
    assert "${cancellationNotice(job)}" in tasks
    assert "cancel_effective:job.cancel_effective" in tasks


def test_feature_panel_uses_all_declared_features_and_keeps_missing_values_visible():
    run_module(r"""
import assert from 'node:assert/strict';
import {mount} from './pages/analysis.js';
function element(){return {value:'',textContent:'读取特征',innerHTML:'',isConnected:true,hidden:false,listeners:new Map(),addEventListener(kind,callback){this.listeners.set(kind,callback);},removeEventListener(){},setAttribute(){},after(){},replaceChildren(){this.innerHTML='';},querySelector(){return element();}};}
const nodes=new Map();
const host={...element(),querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);},querySelectorAll(){return [];}};
globalThis.document={createElement:element};
globalThis.cancelAnimationFrame=()=>{};
const featureColumns=Array.from({length:20},(_,index)=>`feature_${String(index).padStart(2,'0')}`);
const raw={open:10,high:12,low:9,close:11,volume:100,amount:1000,ma5:10,ma10:10,ma20:10,ma60:10};
const latest={...raw,score_input_eligible:false,score_reason_codes:['calendar_gap']};
for(const [index,column] of featureColumns.entries())if(index<19)latest[column]=index/100;
const features={profile:'ma_trend_v1',columns:[...Object.keys(raw),...featureColumns],feature_columns:featureColumns,rows:[latest]};
globalThis.fetch=async url=>({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>url.includes('/reports/')?{symbol:'600519.SH',data_end:'2026-10-20',frequencies:{}}:features});
const signal=new AbortController().signal;
const cleanup=await mount(host,{signal,params:new URLSearchParams({report:'saved'})});
const button=nodes.get('[data-features]');
await button.listeners.get('click')({currentTarget:button});
const html=nodes.get('[data-feature-output]').innerHTML;
const visible=html.split('<div class="schema-list">')[0];
for(const column of featureColumns)assert(visible.includes(column),column);
assert.equal((visible.match(/<strong>/g) || []).length,20);
assert(visible.includes('<strong>—</strong>'));
assert(!visible.includes('<span>open</span>'));
assert(visible.includes('特征输入未通过'));
assert(visible.includes('calendar_gap'));
assert(html.includes('feature_columns'));
assert(html.includes('score_reason_codes'));
cleanup();
""")


def test_full_scan_population_paginates_rows_after_two_hundred():
    run_module(r"""
import assert from 'node:assert/strict';
import {scanPage} from './pages/scan.js';
const rows=Array.from({length:5037},(_,index)=>({symbol:`${index}`,score:index,status:index===5000?'missing':'scored'}));
let view=scanPage(rows,{page:51,pageSize:100});
assert.equal(view.received,5037);assert.equal(view.total,5037);assert.equal(view.rows.length,37);assert.equal(view.rows.at(-1).symbol,'5036');
view=scanPage(rows,{status:'missing'});assert.equal(view.total,1);assert.equal(view.rows[0].symbol,'5000');
view=scanPage(rows,{query:'5036'});assert.equal(view.rows[0].symbol,'5036');
""")


def test_model_selection_requires_explicit_frozen_identity_and_preserves_links():
    run_module(r"""
import assert from 'node:assert/strict';
import {modelOptions,modelLinks,seriesOptions} from './core/models.js';
const models=[{model_id:'frozen-one',model_type:'mlp',feature_count:43,information_cutoff:'2026-10-20',series_id:'series-one'},{model_id:'frozen-two',model_type:'ridge',feature_count:43}];
assert(!modelOptions(models).includes(' selected'));
assert(modelOptions(models,'frozen-two').includes('value="frozen-two" selected'));
const links=modelLinks(models[0]);assert(links.includes('model_id=frozen-one'));assert(links.includes('series_id=series-one'));assert(links.includes('mode=rolling'));
assert(seriesOptions([{series_id:'series-one'}],'series-one').includes('value="series-one" selected'));
""")


def test_unified_training_contract_uses_full_market_and_actual_mlp_options():
    run_module(r"""
import assert from 'node:assert/strict';
import {trainingSpec} from './pages/models.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){const value=this.values[key];return Array.isArray(value)?value[0]:value??null;}getAll(key){return this.values[key]??[];}};
const values={scope:'all',start:'2024-01-01',end:'2026-10-22',backend:'qlib',factor_groups:['price_volume','margin'],models:['ridge','lightgbm','mlp'],horizon:'5',train_sessions:'252',valid_sessions:'63',test_sessions:'63',max_folds:'3',top_k:'10',initial_cash:'1000000',seed:'42',device:'cuda',batch_size:'2048',max_epochs:'50',patience:'6',learning_rate:'.001',weight_decay:'.001',dropout:'.1',cpu_threads:'4'};
const spec=trainingSpec(values);assert.equal(spec.scope,'all');assert.deepEqual(spec.symbols,[]);assert.deepEqual(spec.models,['ridge','lightgbm','mlp']);assert.equal(spec.model_options.max_epochs,50);assert.equal(spec.model_options.device,'cuda');assert(!('epochs' in spec.model_options));
assert.throws(()=>trainingSpec({...values,scope:'symbols',symbols:''}),/股票/);
""")


def test_backtest_fixed_and_rolling_identities_are_mutually_exclusive():
    run_module(r"""
import assert from 'node:assert/strict';
import {backtestSpec} from './pages/backtest.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){return this.values[key]??null;}};
const fields={start:'2026-10-21',end:'2026-10-22',top_k:'10',initial_cash:'1000000',model_id:'model-fixed',series_id:'series-frozen'};
let spec=backtestSpec({...fields,mode:'frozen'});assert.equal(spec.model_id,'model-fixed');assert(!('series_id' in spec));
spec=backtestSpec({...fields,mode:'rolling'});assert.equal(spec.series_id,'series-frozen');assert(!('model_id' in spec));
assert.throws(()=>backtestSpec({...fields,mode:'rolling',series_id:''}),/序列/);
spec=backtestSpec({...fields,mode:'frozen',scope:'symbols',symbols:'600519.SH,000001.SZ'});assert.equal(spec.scope,'symbols');assert.deepEqual(spec.symbols,['600519.SH','000001.SZ']);
spec=backtestSpec({...fields,mode:'frozen',scope:'all'});assert.equal(spec.scope,'all');assert.deepEqual(spec.symbols,[]);
spec=backtestSpec({...fields,mode:'rolling',scope:'symbols',symbols:'600519.SH'});assert(!('scope' in spec));assert(!('symbols' in spec));
""")


def test_ten_year_study_freezes_windows_seeds_and_separates_confirmation():
    run_module(r"""
import assert from 'node:assert/strict';
import {studySpec,studyReport,releaseSummary} from './core/study.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){const value=this.values[key];return Array.isArray(value)?value[0]:value??null;}getAll(key){return this.values[key]??[];}has(key){return Boolean(this.values[key]);}};
const values={start:'2016-10-23',end:'2026-10-22',train_windows:['756','1260'],models:['ridge','mlp'],backend:'qlib',include_margin:true,valid_sessions:'126',test_sessions:'63',holdout_sessions:'252',horizon:'5',top_k:'10',initial_cash:'1000000',device:'cuda',max_epochs:'50'};
const spec=studySpec(values);assert.deepEqual(spec.train_windows,[756,1260]);assert.deepEqual(spec.seeds,[42,137,2026]);assert.equal(spec.holdout_sessions,252);assert.equal(spec.model_options.device,'cuda');assert.equal(spec.scope,'all');
assert.throws(()=>studySpec({...values,models:[]}),/至少/);
const html=studyReport({status:'completed',study_id:'study-saved',selected_protocol:{model_type:'ridge',train_sessions:756},candidates:[{protocol_id:'ridge',development:{median_year_excess:-.02},confirmation:{median_year_excess:-.04}}]});assert(html.includes('-2%'));assert(html.includes('-4%'));assert(html.includes('最新重拟合权重待前瞻'));assert(html.includes('data-publish-study'));
assert(releaseSummary({release_id:'<script>',model_id:'fixed',protocol:{}}).includes('&lt;script&gt;'));
""")


def test_external_study_progress_is_visible_and_never_a_global_percent():
    run_module(r"""
import assert from 'node:assert/strict';
import {studyProgress,studyReport} from './core/study.js';
import {progressValue} from './core/ui.js';
const running={status:'running',study_id:'external',updated_at:'2026-10-23T07:13:52+00:00',progress:{phase:'study_fold',protocol_id:'pv38-ridge-w756',seed:42,current:17,total:17,segment:'confirmation'}};
const html=studyProgress(running);assert(html.includes('pv38-ridge-w756 / 42'));assert(html.includes('17 / 17'));assert(html.includes('历史留出复核'));assert(html.includes('2026-10-23 15:13:52'));assert(html.includes('组合、费用和基准评估'));assert(!html.includes('100%'));assert(html.includes('仅此协议与种子'));
assert.equal(progressValue({kind:'research_study',progress:running.progress}),null);assert.equal(progressValue({kind:'scan',progress:{current:1,total:2}}),50);
const card=studyReport(running).split('<details')[0];assert(card.includes('17 / 17'));assert(card.includes('pv38-ridge-w756'));assert(!card.includes('data-study-resume'));
assert(studyProgress(running,{compact:true}).includes('种子 42'));assert(studyProgress({...running,status:'interrupted'}).includes('上次运行的保存点'));assert(!studyProgress({...running,status:'failed'}).includes('仍在运行'));
assert(studyProgress({status:'running',progress:{phase:'study_features',current:200,total:3862}}).includes('已处理股票'));assert(studyProgress({status:'completed'}).includes('查看研究报告'));
assert(studyProgress({...running,progress:{...running.progress,protocol_id:'<script>'}}).includes('&lt;script&gt;'));
""")


def test_study_final_seed_ranking_is_visible_separately_from_all_candidates():
    run_module(r"""
import assert from 'node:assert/strict';
import {studyReport} from './core/study.js';
const report={status:'completed',study_id:'study-saved',selected_protocol:{model_type:'mlp',train_sessions:756},selection:{publication_seed:42},candidates:[{protocol_id:'initial-ridge',development:{median_year_excess:.02}}],seed_aggregates:[{protocol_id:'reviewed-mlp',seeds_reviewed:[42,137,2026],development:{median_year_excess:-.03,max_drawdown:.2},seed_median_excess_std:.01}]};
const html=studyReport(report).split('<details')[0];assert(html.includes('多种子复核与最终排序'));assert(html.includes('42 / 137 / 2026'));assert(html.includes('reviewed-mlp'));assert(html.includes('initial-ridge'));assert(html.includes('-3%'));assert(html.includes('发布权重固定用种子 42'));assert(html.indexOf('复核协议')<html.indexOf('候选协议'));
""")


def test_training_views_request_summaries_and_full_report_download_is_explicit():
    run_module(r"""
import assert from 'node:assert/strict';
import {api} from './core/api.js';
import {fullReportLink} from './core/ui.js';
import {learningReport} from './pages/models.js';
const calls=[];globalThis.fetch=async(url,options)=>{calls.push({url,options});return{ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.learningRun('learn-saved');await api.study('study-saved');await api.report('backtest-saved');
assert.equal(calls[0].url,'/api/v1/learning/runs/learn-saved?view=dashboard');assert.equal(calls[1].url,'/api/v1/learning/studies/study-saved?view=dashboard');assert.equal(calls[2].url,'/api/v1/reports/backtest-saved?view=dashboard');assert(calls.every(row=>!row.options.method));
const report={run_id:'learn-saved',dashboard_view:{kind:'dashboard_summary',full_report_url:'/api/v1/learning/runs/learn-saved'},models:[],folds:[]};
const html=learningReport(report);assert(html.includes('界面摘要与产物校验'));assert(!html.includes('完整报告与产物校验'));assert(html.includes('href="/api/v1/learning/runs/learn-saved" download'));assert(html.includes('下载完整报告 JSON'));assert.equal(calls.length,3);
assert.equal(fullReportLink({}), '');assert.equal(fullReportLink({dashboard_view:{full_report_url:'javascript:alert(1)'}}),'');
""")


def test_historical_and_replay_requests_preserve_their_evidence_modes():
    run_module(r"""
import assert from 'node:assert/strict';
import {backtestSpec} from './pages/backtest.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){return this.values[key]??null;}};
const values={start:'2018-01-01',end:'2026-10-22',top_k:'10',initial_cash:'1000000',model_id:'fixed'};
let spec=backtestSpec({...values,mode:'historical'});assert.equal(spec.release_id,'active');assert(!('model_id' in spec));
spec=backtestSpec({...values,mode:'historical',study_id:'study-frozen'});assert.equal(spec.study_id,'study-frozen');assert(!('release_id' in spec));
spec=backtestSpec({...values,mode:'research_replay',version_source:'active'});assert.equal(spec.mode,'research_replay');assert.equal(spec.release_id,'active');assert(!('model_id' in spec));
""")


def test_snapshot_import_preserves_lots_and_discards_only_saved_identity_metadata():
    run_module(r"""
import assert from 'node:assert/strict';
import {cleanSnapshot,accountSnapshot,planReport} from './core/trading.js';
const snapshot={as_of:'2026-10-22',cash:100,positions:[{symbol:'600000.SH',total_shares:100,sellable_shares:0,buy_lots:[{date:'2026-10-22',shares:100}]}],permissions:{star:false},source:'manual',is_fixture:false,snapshot_id:'saved',account_id:'account'};
assert.deepEqual(cleanSnapshot(snapshot).positions[0].buy_lots,snapshot.positions[0].buy_lots);assert(!('snapshot_id' in cleanSnapshot(snapshot)));
globalThis.FormData=class{constructor(values){this.values=values;}get(key){return this.values[key]??null;}};
assert.deepEqual(accountSnapshot({source_mode:'json',snapshot_json:JSON.stringify({snapshot})}),cleanSnapshot(snapshot));
assert(planReport({plan_id:'plan',is_fixture:true,orders:[],holds:[]}).includes('合成验收账户'));
""")


def test_data_operations_preserve_typed_controls_and_require_confirmed_review():
    run_module(r"""
import assert from 'node:assert/strict';
import {operationField,operationParams} from './core/data-ops.js';
const fields=[{name:'enabled',kind:'bool'},{name:'datasets',kind:'multi',choices:['daily_bars','margin_trading']},{name:'trade_date',kind:'date'},{name:'groups',kind:'choice',choices:[{value:'one',label:'<unsafe>'}]}];
assert(operationField(fields[3],'one').includes('&lt;unsafe&gt;'));assert(operationField(fields[1],['daily_bars']).includes('multiple'));
globalThis.FormData=class{get(key){return key==='trade_date'?'2026-10-22':null;}getAll(key){return key==='datasets'?['daily_bars','margin_trading']:[];}has(key){return key==='enabled';}};
assert.deepEqual(operationParams({},fields),{enabled:true,datasets:['daily_bars','margin_trading'],trade_date:'2026-10-22'});
""")
    source=(FRONTEND / "core/data-ops.js").read_text(encoding="utf-8")
    assert "storageReview" in source
    assert "value.confirm_enabled===true" in source
    assert "maintenance_confirmed:values.has" in source


def test_data_operation_fields_use_shanghai_today_and_keep_pack_values():
    run_module(r"""
import assert from 'node:assert/strict';
import {operationField,operationParams} from './core/data-ops.js';
const RealDate=Date;
globalThis.Date=class extends RealDate{constructor(...values){super(...(values.length?values:['2026-10-22T16:30:00Z']));}};
assert(operationField({name:'trade_date',kind:'date'}).includes('value="2026-10-23"'));
assert(operationField({name:'trade_date',kind:'date'},'2026-09-01').includes('value="2026-09-01"'));
assert(operationField({name:'start',kind:'date'}).includes('value=""'));
const toggle=operationField({name:'backfill',kind:'bool',label:'补跑 <检查>',help:'仅重跑 <指定日期>'},true);
assert(toggle.includes(' checked'));assert(toggle.includes('补跑 &lt;检查&gt;'));
assert(toggle.includes('仅重跑 &lt;指定日期&gt;'));assert(!toggle.includes('<指定日期>'));
const pack={name:'pack',kind:'multi',choices:['market','fundamentals','universe']};
const html=operationField(pack,['market','fundamentals']);
assert(html.includes('value="market" selected>行情（核心组）'));
assert(html.includes('value="fundamentals" selected>基本面'));
assert(html.includes('value="universe">历史 ST 范围（无日更组）'));
globalThis.FormData=class{get(){return null;}getAll(){return ['market','fundamentals'];}};
assert.deepEqual(operationParams({},[pack]),{pack:['market','fundamentals']});
globalThis.FormData=class{get(){return null;}getAll(){return [];}};
assert.deepEqual(operationParams({},[pack]),{pack:[]});
""")


def test_data_operation_occupancy_distinguishes_jobs_and_actual_maintenance():
    run_module(r"""
import assert from 'node:assert/strict';
import {operationOccupancyHtml} from './core/data-ops.js';
const daily=operationOccupancyHtml({engine_locks:['daily_ingestion'],local_job_busy:true,storage_busy:false});
assert(daily.includes('日更采集正在运行'));assert(daily.includes('共用一个采集锁'));
assert(daily.includes('href="#tasks"'));assert(!daily.includes('存储维护'));
assert(!daily.includes('本地数据或研究任务正在运行'));
const local=operationOccupancyHtml({local_job_busy:true,storage_busy:false});
assert(local.includes('本地数据或研究任务正在运行'));assert(!local.includes('存储维护'));
const maintenance=operationOccupancyHtml({engine_locks:['daily_ingestion'],storage_busy:true});
assert(maintenance.includes('存储维护正在运行'));assert(maintenance.includes('日更采集正在运行'));
assert.equal(operationOccupancyHtml({storage_busy:false,local_job_busy:false}), '');
assert.equal(operationOccupancyHtml(), '');
""")


def test_opening_data_operations_reads_occupancy_without_starting_an_operation():
    run_module(r"""
import assert from 'node:assert/strict';
import {mountDataOps} from './core/data-ops.js';
function element(){return {innerHTML:'',listeners:new Map(),isConnected:true,addEventListener(kind,callback){this.listeners.set(kind,callback);},setAttribute(){},replaceChildren(){this.innerHTML='';},insertAdjacentHTML(){}};}
const form=element(),select=element(),description=element(),fields=element(),content=element(),result=element();
content.querySelector=selector=>({'[name=operation]':select,'[data-operation-form]':form,'[data-op-description]':description,'[data-op-fields]':fields})[selector];
const host={...element(),querySelector:selector=>({'[data-data-ops-content]':content,'[data-data-ops-preview]':result})[selector],querySelectorAll:()=>[]};
const calls=[];
globalThis.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({operations:[{id:'daily.full',title:'日更',params:[{name:'trade_date',kind:'date'},{name:'no_events',kind:'bool',help:'跳过事件流'}]}],occupancy:{engine_locks:['daily_ingestion'],storage_busy:false,local_job_busy:true}})};};
globalThis.FormData=class{get(){return 'daily.full';}};
const controller=new AbortController(),cleanup=await mountDataOps(host,{signal:controller.signal});
assert(content.innerHTML.includes('日更采集正在运行'));assert(content.innerHTML.includes('href="#tasks"'));
select.listeners.get('change')();
assert.match(fields.innerHTML,/name="trade_date" type="date" value="\d{4}-\d{2}-\d{2}"/);
assert(fields.innerHTML.includes('跳过事件流'));
assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/v1/data/operations');
assert.equal(calls[0].options.method,undefined);
controller.abort();cleanup();
""")


def test_prediction_chart_and_data_operations_use_only_unified_api_paths():
    run_module(r"""
import assert from 'node:assert/strict';
import {api} from './core/api.js';
const calls=[];globalThis.fetch=async(url,options)=>{calls.push({url,options});return{ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.predictionChart('saved-one');await api.operations();await api.operationPreview({op:'daily.full',params:{}});await api.learningPreflight({scope:'all'});
assert.equal(calls[0].url,'/api/v1/predictions/saved-one/chart');assert.equal(calls[1].url,'/api/v1/data/operations');assert.equal(calls[2].url,'/api/v1/data/operations/preview');assert.equal(calls[3].url,'/api/v1/learning/preflight');
assert.equal(calls[1].options.method,undefined);
""")


def test_final_refit_is_explicit_and_new_inference_version_has_no_oos_claim():
    run_module(r"""
import assert from 'node:assert/strict';
import {learningReport} from './pages/models.js';
import {modelSummary,modelLinks} from './core/models.js';
const html=learningReport({family:'cross_sectional_return_v1',run_id:'learn-frozen',models:[{name:'factor_mlp',metrics:{}}],spec:{},folds:[],feature_groups:{}});
assert(html.includes('data-refit-run="learn-frozen"'));assert(html.includes('data-refit-name="factor_mlp"'));assert(html.includes('按此协议最终重拟合'));
const model={model_id:'refit-one',model_type:'mlp',usage:'frozen_inference_only',qualification:{strict_history_eligible:false}};
assert(modelSummary(model).includes('没有独立样本外成绩'));assert(!modelLinks(model).includes('mode=rolling'));
""")


def test_data_inspection_reads_new_routes_and_preserves_period_granularity():
    run_module(r"""
import assert from 'node:assert/strict';
import {api} from './core/api.js';
import {heatmapHtml} from './core/data-inspection.js';
const calls=[];globalThis.fetch=async(url,options)=>{calls.push({url,options});return{ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.datasetDetail('daily_bars');await api.datasetPartitions('daily_bars');await api.datasetDates('daily_bars');await api.datasetProvenance('daily_bars');await api.datasetProvenanceSeries('daily_bars');await api.qualityRun('quality-one');await api.dataRuns({limit:30,offset:30});await api.dataRun('run-one');await api.dataHeatmap({days:14});await api.dataset('daily_bars',{adjust:'hfq',pit_mode:'strict'});
assert.equal(calls[0].url,'/api/v1/data/datasets/daily_bars');assert.equal(calls[4].url,'/api/v1/data/datasets/daily_bars/provenance/series');assert.equal(calls[5].url,'/api/v1/data/quality/runs/quality-one');assert.equal(calls[6].url,'/api/v1/data/runs?limit=30&offset=30');assert.equal(calls[8].url,'/api/v1/data/heatmap?days=14');assert(calls[9].url.includes('adjust=hfq&pit_mode=strict'));assert(calls.every(row=>row.options.method===undefined));
const html=heatmapHtml({days:['2026-10-22'],legend:{'#':'covered'},rows:[{dataset:'events',granularity:'month',cells:'#',gap_meaning:'cadence'}]});assert(html.includes('month'));assert(html.includes('不能据此认定逐日观测完整'));assert(html.includes('data-coverage-state="covered"'));assert(html.includes('2026-10-22 · 分区覆盖 · 原始粒度 month'));
""")


def test_storage_confirmation_requires_ready_exact_objects_and_enabled_capability():
    run_module(r"""
import assert from 'node:assert/strict';
import {storageReviewHtml} from './core/data-ops.js';
const result={job_id:'review-one',status:'ready',plan_id:'plan-one',confirmation_token:'one-use-token',logical_bytes:1234,objects:[{object_id:'revision-exact',label:'精确旧版本',logical_bytes:1234}]};
assert(!storageReviewHtml(result,false).includes('data-storage-confirm'));
assert(!storageReviewHtml({...result,status:'checking'},true).includes('data-storage-confirm'));
const html=storageReviewHtml(result,true);assert(html.includes('revision-exact'));assert(html.includes('1,234'));assert(html.includes('name="confirmed" required'));assert(html.includes('name="maintenance_confirmed" required'));assert(!html.includes('checked'));
""")


def test_next_natural_day_is_strict_iso_calendar_arithmetic():
    run_module(r"""
import assert from 'node:assert/strict';
import {nextNaturalDay} from './core/ui.js';
assert.equal(nextNaturalDay('2026-10-21'),'2026-10-22');
assert.equal(nextNaturalDay('2026-12-31'),'2027-01-01');
assert.equal(nextNaturalDay('2027-02-28'),'2027-03-01');
assert.equal(nextNaturalDay('2028-02-28'),'2028-02-29');
assert.equal(nextNaturalDay('2028-02-29'),'2028-03-01');
assert.equal(nextNaturalDay('2026-02-29'),'');
assert.equal(nextNaturalDay('2026-02-30'),'');
assert.equal(nextNaturalDay('2026-04-31'),'');
assert.equal(nextNaturalDay('2026-13-01'),'');
assert.equal(nextNaturalDay('2026-00-10'),'');
assert.equal(nextNaturalDay('2026-1-5'),'');
assert.equal(nextNaturalDay('2026-10-21T00:00:00Z'),'');
assert.equal(nextNaturalDay(''),'');
assert.equal(nextNaturalDay(null),'');
assert.equal(nextNaturalDay(undefined),'');
""")


def test_default_model_identity_uses_active_release_and_flags_missing_model_detail():
    run_module(r"""
import assert from 'node:assert/strict';
import {defaultModelIdentity} from './core/models.js';
const active={model_id:'model-aad295f46480b5cd09cc488f71a5c80d',model_type:'mlp',information_cutoff:'2026-10-21'};
const response={models:[{model_id:'older'},active],research_active:{release_id:'release-b43d198c',model_id:active.model_id,information_cutoff:'2026-10-21'}};
const frozen=JSON.stringify(response);
const identity=defaultModelIdentity(response);
assert.equal(identity.release.release_id,'release-b43d198c');assert.equal(identity.modelId,active.model_id);assert.equal(identity.model,active);assert.equal(identity.source,'active_release');assert.equal(identity.detailMissing,false);
assert.equal(JSON.stringify(response),frozen);
const archived=defaultModelIdentity({models:[{model_id:'older'}],research_active:{release_id:'release-b43d198c',model_id:active.model_id}});
assert.equal(archived.release.release_id,'release-b43d198c');assert.equal(archived.modelId,active.model_id);assert.equal(archived.model,null);assert.equal(archived.source,'active_release');assert.equal(archived.detailMissing,true);
const missing=defaultModelIdentity({models:[active],research_active:{release_id:'release-b43d198c'}});
assert.equal(missing.modelId,'');assert.equal(missing.model,null);assert.equal(missing.detailMissing,true);
const none=defaultModelIdentity({models:[active],research_active:null,series:[],assets:{}});
assert.deepEqual(none,{release:null,model:null,modelId:'',source:'no_default_method',detailMissing:false});
assert.deepEqual(defaultModelIdentity(),{release:null,model:null,modelId:'',source:'no_default_method',detailMissing:false});
""")


def test_watchlist_selection_helpers_scope_links_to_the_default_method():
    run_module(r"""
import assert from 'node:assert/strict';
import {selectedWatchSymbols,watchlistLinks} from './pages/watchlist.js';
const rows=[{symbol:'002436.SZ'},{symbol:'600862.SH'},{symbol:'600869.SH'}];
const selected=new Set(['600862.SH','999999.SZ','002436.SZ','000420.SZ']);
assert.deepEqual(selectedWatchSymbols(selected,rows),['002436.SZ','600862.SH']);
assert.deepEqual(selectedWatchSymbols(['000420.SZ','600862.SH'],[...rows,{symbol:'000420.SZ'}]),['600862.SH','000420.SZ']);
assert.deepEqual(selectedWatchSymbols(new Set(),rows),[]);
assert.deepEqual(selectedWatchSymbols(selected,[]),[]);
const links=watchlistLinks(['002436.SZ','600862.SH']);
assert(links.backtest.startsWith('#backtest?'));assert(links.backtest.includes('release_id=active'));assert(links.backtest.includes('mode=frozen'));
assert(links.backtest.includes('002436.SZ'));assert(links.backtest.includes('600862.SH'));assert(!links.backtest.includes('model_id='));
assert(links.scan.startsWith('#scan?'));assert(links.scan.includes('release_id=active'));assert(links.scan.includes('002436.SZ'));assert(links.scan.includes('600862.SH'));assert(!links.scan.includes('mode=frozen'));assert(!links.scan.includes('model_id='));
assert.equal(watchlistLinks([]).scan,'#scan?release_id=active');
""")


def test_frozen_default_method_backtest_spec_keeps_the_watchlist_pool():
    run_module(r"""
import assert from 'node:assert/strict';
import {backtestSpec} from './pages/backtest.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){return this.values[key]??null;}};
const fields={start:'2026-10-22',end:'2026-10-24',top_k:'10',initial_cash:'1000000',version_source:'active',scope:'symbols',symbols:'002436.SZ,600862.SH'};
const spec=backtestSpec({...fields,mode:'frozen'});
assert.equal(spec.mode,'frozen');assert.equal(spec.release_id,'active');assert.equal(spec.scope,'symbols');assert.deepEqual(spec.symbols,['002436.SZ','600862.SH']);assert(!('model_id' in spec));assert.equal(spec.start,'2026-10-22');
assert.throws(()=>backtestSpec({...fields,mode:'frozen',symbols:''}),/股票/);
assert.throws(()=>backtestSpec({...fields,mode:'frozen',symbols:'   '}),/股票/);
const training=backtestSpec({...fields,mode:'frozen',scope:'training'});
assert.equal(training.release_id,'active');assert(!('scope' in training));assert(!('symbols' in training));assert(!('model_id' in training));
""")

