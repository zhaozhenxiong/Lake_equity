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
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_version_change_detection_only_flags_a_real_change():
    run_module(r"""
import assert from 'node:assert/strict';
import {frontendVersion,versionChanged,showVersionBanner} from './app.js';
assert.equal(frontendVersion({frontend_version:'abc123'}),'abc123');
assert.equal(frontendVersion({data:{frontend_version:' nested '}}),'nested');
assert.equal(frontendVersion({}),'');
assert.equal(frontendVersion(),'');
assert.equal(versionChanged('abc123','abc123'),false);
assert.equal(versionChanged('abc123','def456'),true);
assert.equal(versionChanged('','def456'),false);
assert.equal(versionChanged('abc123',''),false);
assert.equal(versionChanged(undefined,undefined),false);
assert.equal(versionChanged(null,'abc456'),false);
assert.equal(showVersionBanner(null),false);
const banner={hidden:true};
assert.equal(showVersionBanner(banner),true);
assert.equal(banner.hidden,false);
""")
    shell = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "data-version-banner" in shell
    assert "工作台有新版本，点击刷新" in shell
    assert 'role="status"' in shell
    assert "data-version-reload" in shell
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "data-version-reload" in app
    assert "location.reload()" in app
    assert "window.addEventListener('hashchange',()=>{route();checkVersion();});" in app
    assert "setInterval(checkVersion,60000)" in app
    assert "request('/health')" in app
    # 只在版本变化后显示横幅，绝不自动刷新页面。
    assert "location.reload()" in app and "checkVersion" in app
    stylesheet = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert ".version-banner" in stylesheet
    assert ".version-banner[hidden]{display:none}" in stylesheet


def test_report_cohort_effective_renders_only_when_present():
    run_module(r"""
import assert from 'node:assert/strict';
import {cohortEffectiveHtml,modelBacktestExtrasHtml} from './core/reports.js';
assert.equal(cohortEffectiveHtml({}),'');
assert.equal(cohortEffectiveHtml({family:'model_backtest_v1'}),'');
assert(!modelBacktestExtrasHtml({family:'model_backtest_v1',status:'completed',run_id:'backtest-old'}).includes('data-cohort-effective'));
const result={family:'model_backtest_v1',status:'completed',run_id:'backtest-opt',cohort_effective:{requested:5,scored:3,universe_scope:'release',cap_applied:true,release_universe_policy:{version:'liquidity_coverage_v1',max_members:1000},excluded:[{symbol:'600862.SH',reason:'rank_beyond_cap',rank:1493,amount60_median:213916140.5},{symbol:'<img src=x>',reason:'liquidity_below_threshold',rank:null,amount60_median:null}],unavailable:[{symbol:'000420.SZ',reason:'not_in_saved_evaluation_data'}]}};
const html=cohortEffectiveHtml(result);
assert(html.includes('data-cohort-effective'));
assert(html.includes('有效评分 3/5'));
assert(html.includes('600862.SH'));
assert(html.includes('rank_beyond_cap'));
assert(html.includes('1,493'));
assert(html.includes('000420.SZ'));
assert(html.includes('not_in_saved_evaluation_data'));
assert(html.includes('默认释放股票池'));
assert(html.includes('已应用 1000 只上限'));
assert(html.includes('&lt;img src=x&gt;'));
assert(!html.includes('<img src=x>'));
const explicit=cohortEffectiveHtml({cohort_effective:{requested:2,scored:2,universe_scope:'explicit_symbols',cap_applied:false,excluded:[],unavailable:[]}});
assert(explicit.includes('有效评分 2/2'));
assert(explicit.includes('显式股票池'));
assert(explicit.includes('未应用'));
const extras=modelBacktestExtrasHtml(result,{runId:'backtest-opt'});
assert(extras.includes('data-cohort-effective'));
assert(extras.includes('data-plan-generate="backtest-opt"'));
assert(modelBacktestExtrasHtml({family:'equity_curve_v1'}).length===0);
""")
    reports = (FRONTEND / "core" / "reports.js").read_text(encoding="utf-8")
    assert "cohortEffectiveHtml(result)" in reports
    assert "modelBacktestExtrasHtml(result,{runId:reportId(result)})" in reports


def test_plan_action_requires_explicit_confirmation_before_request():
    run_module(r"""
import assert from 'node:assert/strict';
import {PLAN_CONFIRM_TEXT,isCompletedModelBacktest,planActionHtml,planConfirmHtml,runsBacktestPlanRequest} from './core/reports.js';
assert.equal(PLAN_CONFIRM_TEXT,'仅研究计划，不下单、不写账户；使用回测期末研究账户');
const completed={family:'model_backtest_v1',status:'completed',run_id:'backtest-abc'};
assert.equal(isCompletedModelBacktest(completed),true);
assert.equal(isCompletedModelBacktest({...completed,status:'running'}),false);
assert.equal(isCompletedModelBacktest({...completed,family:'backtest_plan_v1'}),false);
assert.equal(isCompletedModelBacktest({...completed,kind:'bplan'}),false);
assert.equal(isCompletedModelBacktest({...completed,status:'completed'}),true);
assert.equal(isCompletedModelBacktest({family:'model_backtest_v1',status:'completed'}),true);
const action=planActionHtml(completed);
assert(action.includes('生成次日计划'));
assert(action.includes('data-plan-generate="backtest-abc"'));
assert(action.includes('data-plan-zone'));
assert(!action.includes('data-plan-confirm'));
assert.equal(planActionHtml({...completed,status:'running'}),'');
assert.equal(planActionHtml({...completed,run_id:''}),'');
const confirm=planConfirmHtml('backtest-abc');
assert(confirm.includes('仅研究计划，不下单、不写账户；使用回测期末研究账户'));
assert(confirm.includes('data-plan-confirm="backtest-abc"'));
assert(confirm.includes('data-plan-cancel'));
assert(!confirm.includes('data-plan-generate'));
assert.equal(planConfirmHtml(''),'');
// 首次点击只显示确认：confirmed 不是显式 true 时绝不发请求。
assert.equal(runsBacktestPlanRequest({runId:'backtest-abc',confirmed:false}),false);
assert.equal(runsBacktestPlanRequest({runId:'backtest-abc'}),false);
assert.equal(runsBacktestPlanRequest({runId:'',confirmed:true}),false);
assert.equal(runsBacktestPlanRequest({runId:'   ',confirmed:true}),false);
assert.equal(runsBacktestPlanRequest({runId:'backtest-abc',confirmed:true}),true);
""")
    reports = (FRONTEND / "core" / "reports.js").read_text(encoding="utf-8")
    assert "api.backtestPlan(runId)" in reports
    assert "async event=>" in reports
    assert "data-plan-confirm" in reports


def test_plan_action_first_click_only_confirms_then_requests_once():
    run_module(r"""
import assert from 'node:assert/strict';
import {renderReport} from './core/reports.js';
const calls=[];
globalThis.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({family:'backtest_plan_v1',schema:'backtest_conditional_plan_v1',kind:'bplan',plan_id:'bplan-abc',source_backtest_run_id:'backtest-abc',signal_date:'2026-10-23',execution_date:'2026-10-26',account_scope:'backtest_terminal_research_account',orders:[{symbol:'600519.SH',side:'BUY',quantity:100,limit_price:1500.5}],holds:[],limitations:[]})};};
function stub(){const node={innerHTML:'',textContent:'',dataset:{},hidden:false,listeners:new Map(),addEventListener(kind,callback){node.listeners.set(kind,callback);},querySelector(){return stub();}};return node;}
const zone=stub(),accountSelect=stub(),accountContent=stub(),equityHost=stub();
accountContent.querySelector=selector=>selector==='[data-equity]'?equityHost:stub();
const content=stub();
content.insertAdjacentHTML=function(){};
content.querySelector=selector=>({'[data-plan-zone]':zone,'[data-account]':accountSelect,'[data-account-content]':accountContent})[selector]||stub();
const host=stub();host.querySelector=selector=>selector==='[data-report-content]'?content:stub();
const report={family:'model_backtest_v1',status:'completed',run_id:'backtest-abc',mode:'frozen',portfolio:{daily:[],trades:[],metrics:{}},start:'2026-10-22',end:'2026-10-24'};
const cleanup=renderReport(host,report,{id:'backtest-abc'});
assert(content.listeners.has('click'));
const click=content.listeners.get('click');
await click({target:{closest:selector=>selector==='[data-plan-generate]'?{dataset:{planGenerate:'backtest-abc'}}:null}});
assert.equal(calls.length,0);
assert(zone.innerHTML.includes('仅研究计划，不下单、不写账户'));
await click({target:{closest:selector=>selector==='[data-plan-confirm]'?{dataset:{planConfirm:'backtest-abc'}}:null}});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(calls.length,1);
assert.equal(calls[0].url,'/api/v1/backtests/backtest-abc/plan');
assert.equal(calls[0].options.method,'POST');
assert.deepEqual(JSON.parse(calls[0].options.body),{confirm:true});
assert.equal(calls[0].options.credentials,'same-origin');
assert(zone.innerHTML.includes('backtest_terminal_research_account'));
assert(zone.innerHTML.includes('600519.SH'));
cleanup();
""")


def test_backtest_plan_report_renders_research_account_disclaimer_orders_and_source_link():
    run_module(r"""
import assert from 'node:assert/strict';
import {backtestPlanHtml,renderReport} from './core/reports.js';
const plan={schema:'backtest_conditional_plan_v1',family:'backtest_plan_v1',kind:'bplan',plan_id:'bplan-0123456789abcdef0123456789abcdef',run_id:'bplan-0123456789abcdef0123456789abcdef',source_backtest_run_id:'backtest-source',signal_date:'2026-10-23',execution_date:'2026-10-26',next_session_source:'verified_trading_calendar',account_scope:'backtest_terminal_research_account',account:{cash:100000,positions:{'600519.SH':100}},selected:['600519.SH'],orders:[{order_id:'order-1',symbol:'600519.SH',side:'BUY',quantity:100,limit_price:1500.5,estimated_notional:150050,estimated_fees:15.01,constraints:['整手','参与率上限'],reason:'frozen_model_rank_rebalance',status:'planned'}],holds:[{symbol:'000001.SZ',reason:'missing_reference_price',current_shares:0,target_weight:0}],valuation_missing_symbols:['000001.SZ'],limitations:['仅研究计划，不构成投资建议','评价人口受关注池限制'],status:'completed'};
const html=backtestPlanHtml(plan);
assert(html.includes('data-backtest-plan'));
assert(html.includes('仅研究计划，不下单、不写账户'));
assert(html.includes('使用回测期末研究账户'));
assert(html.includes('backtest_terminal_research_account'));
assert(html.includes('2026-10-23'));
assert(html.includes('2026-10-26'));
assert(html.includes('600519.SH'));
assert(html.includes('买入'));
assert(html.includes('1,500.5'));
assert(html.includes('150,050'));
assert(html.includes('15.01'));
assert(html.includes('整手'));
assert(html.includes('000001.SZ'));
assert(html.includes('missing_reference_price'));
assert(html.includes('仅研究计划，不构成投资建议'));
assert(html.includes('data-plan-source-backtest="backtest-source"'));
assert(!html.includes('data-plan-open'));
assert.equal(backtestPlanHtml(null),'');
assert(backtestPlanHtml({}).includes('计划没有拟执行订单'));
// renderReport 的 backtest_plan_v1 分支必须走同一渲染器（旧报告分支保持不变）。
const content={innerHTML:'',addEventListener(){},querySelector(){return null;}};
const generic={textContent:'',addEventListener(){},querySelector:()=>null};
const host={innerHTML:'',querySelector(selector){return selector==='[data-report-content]'?content:generic;},querySelectorAll(){return [];}};
const cleanup=renderReport(host,plan,{id:plan.run_id});
assert(content.innerHTML.includes('仅研究计划，不下单、不写账户'));
assert(content.innerHTML.includes('600519.SH'));
assert(content.innerHTML.includes('backtest-source'));
assert.equal(typeof cleanup,'function');
cleanup();
""")
    reports = (FRONTEND / "core" / "reports.js").read_text(encoding="utf-8")
    assert "result.family==='backtest_plan_v1'" in reports
    assert "data-plan-source-backtest" in reports


def test_default_method_explanation_states_identity_and_escapes_html():
    run_module(r"""
import assert from 'node:assert/strict';
import {defaultMethodExplanation} from './core/models.js';
const release={release_id:'release-<b43d198c>',name:'阶段研究主模型 <script>alert(1)</script>',model_id:'model-frozen',series_id:'series-one',information_cutoff:'2026-10-21',protocol:{protocol_id:'pv38-ridge-w756',model_name:'factor_mlp',model_type:'factor_mlp',feature_count:43,train_sessions:756},qualification:{stage:'research_default',research_only:true,paper_allocation_eligible:false,strict_history_eligible:false},activation:{text:'由一键流水线自动启用'},series:{start:'2024-01-02',end:'2026-10-21'}};
const html=defaultMethodExplanation({model_id:'model-frozen',feature_count:43,train_sessions:756},release);
assert(html.includes('data-default-method-explanation'));
assert(html.includes('release-&lt;b43d198c&gt;'));
assert(html.includes('pv38-ridge-w756'));
assert(html.includes('factor_mlp'));
assert(html.includes('43 项'));
assert(html.includes('756 交易日'));
assert(html.includes('2026-10-21'));
assert(html.includes('series-one'));
assert(html.includes('2024-01-02'));
assert(html.includes('由一键流水线自动启用'));
assert(html.includes('为什么这是默认方法'));
assert(html.includes('#models'));
assert(html.includes('&lt;script&gt;'));
assert(!html.includes('<script>'));
assert(!html.includes('&amp;lt;'));
const missing=defaultMethodExplanation(undefined,{release_id:'release-simple'});
assert(missing.includes('release-simple'));
assert(missing.includes('#models'));
assert(!missing.includes('undefined'));
assert(!missing.includes('NaN'));
assert(defaultMethodExplanation().length>0);
""")
    source = (FRONTEND / "core" / "models.js").read_text(encoding="utf-8")
    assert "export function defaultMethodExplanation(model, release)" in source
    assert "href=\"#models\"" in source


def test_report_error_advice_reuses_the_shared_mapping():
    run_module(r"""
import assert from 'node:assert/strict';
import {planErrorHtml,reportErrorAdvice} from './core/reports.js';
const cutoff=reportErrorAdvice(new Error('开始日必须晚于信息截止 2026-10-21'));
assert(cutoff.includes('2026-10-22'));
const cap=reportErrorAdvice(new Error('关注池最多 200 只股票，请先移除再添加。'));
assert(cap.includes('200'));
const series=reportErrorAdvice(new Error('区间必须落在保存的序列覆盖内'));
assert(series.includes('序列'));
const pool=reportErrorAdvice(new Error('显式股票池为空，请先勾选股票'));
assert(pool.includes('关注页'));
assert.equal(reportErrorAdvice(new Error('synthetic failure')),'');
const html=planErrorHtml(new Error('<script>alert(1)</script>'));
assert(html.includes('&lt;script&gt;'));
assert(!html.includes('<script>'));
""")
