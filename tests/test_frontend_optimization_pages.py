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


def test_watchlist_entries_carry_concrete_release_scope_and_product_cap():
    run_module(r"""
import assert from 'node:assert/strict';
import {WATCHLIST_FULL_MESSAGE,WATCHLIST_LIMIT,defaultModelFacts,estimatedTradingDays,watchlistBacktestLinks,watchlistFull,watchlistLinks,watchlistPool,watchlistScanLink} from './pages/watchlist.js';
const codes=['002436.SZ','600862.SH'];
const links=watchlistBacktestLinks(codes,'release-b43d198c3154b4119c2e586ace5f9155');
assert(links.frozen.startsWith('#backtest?'));
assert(links.frozen.includes('release_id=release-b43d198c3154b4119c2e586ace5f9155'));
assert(links.frozen.includes('mode=frozen'));
assert(links.frozen.includes('universe_scope=explicit_symbols'));
assert(links.frozen.includes('002436.SZ'));assert(links.frozen.includes('600862.SH'));
assert(!links.frozen.includes('model_id='));assert(!links.frozen.includes('release_id=active'));
assert(links.replay.includes('mode=research_replay'));
assert(links.replay.includes('release_id=release-b43d198c3154b4119c2e586ace5f9155'));
assert(links.replay.includes('universe_scope=explicit_symbols'));
const replay=watchlistBacktestLinks(codes,'release-x',{hasSeries:true});
assert(replay.replay.includes('mode=watchlist_replay'));assert(!replay.replay.includes('mode=research_replay'));
assert(replay.frozen.includes('release_id=release-x'));
const scan=watchlistScanLink(codes,'release-x');
assert(scan.startsWith('#scan?'));
assert(scan.includes('release_id=release-x'));assert(scan.includes('universe_scope=explicit_symbols'));
assert(scan.includes('002436.SZ'));assert(scan.includes('600862.SH'));assert(!scan.includes('mode=frozen'));
// The legacy default-method link shape is preserved for older callers.
assert.equal(watchlistLinks([]).scan,'#scan?release_id=active');
assert(watchlistLinks(codes).backtest.includes('release_id=active'));
// Concrete-release overload yields both explicit entries.
const concrete=watchlistLinks(codes,'release-b43d198c',{hasSeries:true});
assert(concrete.backtest.includes('release_id=release-b43d198c'));assert(concrete.backtest.includes('mode=frozen'));
assert(concrete.replay.includes('mode=watchlist_replay'));assert(concrete.scan.includes('release_id=release-b43d198c'));
assert(concrete.scan.includes('universe_scope=explicit_symbols'));
// 200-stock product cap: helpers used by add-watch and both entries.
assert.equal(WATCHLIST_LIMIT,200);
assert(WATCHLIST_FULL_MESSAGE.includes('200'));
assert(watchlistFull(Array.from({length:200},(_,index)=>`${index}`)));
assert(!watchlistFull(Array.from({length:199},(_,index)=>`${index}`)));
assert.equal(watchlistFull(),false);
assert.deepEqual(watchlistPool(['a','b']),['a','b']);
assert.throws(()=>watchlistPool(Array.from({length:201},(_,index)=>`${index}`)),/最多 200/);
// Calendar estimate and derived facts (backend calendar still wins).
assert.equal(estimatedTradingDays('2026-10-22','2026-10-28'),5);
assert.equal(estimatedTradingDays('2026-10-24','2026-10-25'),0);
assert.equal(estimatedTradingDays('2026-10-28','2026-10-22'),0);
assert.equal(estimatedTradingDays('bad','2026-10-22'),0);
const facts=defaultModelFacts({release:{information_cutoff:'2026-10-21'}},{todayDate:'2026-10-28'});
assert.equal(facts.cutoff,'2026-10-21');assert.equal(facts.earliestStart,'2026-10-22');
assert.equal(facts.estimatedSessions,5);assert(facts.estimateNote.includes('后端核验'));
assert.equal(defaultModelFacts({release:{information_cutoff:'2026-10-21'}},{todayDate:'2026-10-21'}).earliestStart,'2026-10-22');
assert.equal(defaultModelFacts(null,{todayDate:'2026-10-28'}).earliestStart,'');
""")


def test_backtest_spec_submits_concrete_release_and_watchlist_replay():
    run_module(r"""
import assert from 'node:assert/strict';
import {backtestSpec} from './pages/backtest.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){return this.values[key]??null;}};
const base={start:'2026-10-22',end:'2026-10-24',top_k:'10',initial_cash:'1000000',release_id:'release-b43d198c3154b4119c2e586ace5f9155'};
let spec=backtestSpec({...base,mode:'frozen',scope:'symbols',symbols:'002436.SZ,600862.SH',version_source:'active',universe_scope:'explicit_symbols'});
assert.equal(spec.mode,'frozen');assert.equal(spec.release_id,'release-b43d198c3154b4119c2e586ace5f9155');
assert.equal(spec.scope,'symbols');assert.deepEqual(spec.symbols,['002436.SZ','600862.SH']);
assert.equal(spec.universe_scope,'explicit_symbols');assert(!('model_id' in spec));
spec=backtestSpec({...base,mode:'watchlist_replay',scope:'symbols',symbols:'002436.SZ',universe_scope:'explicit_symbols'});
assert.equal(spec.mode,'watchlist_replay');assert.equal(spec.release_id,base.release_id);
assert.deepEqual(spec.symbols,['002436.SZ']);assert(!('model_id' in spec));assert(!('series_id' in spec));
// No explicit release falls back to active without rewriting a provided concrete id.
assert.equal(backtestSpec({start:'2026-10-22',end:'2026-10-24',top_k:'10',initial_cash:'1000000',mode:'frozen',scope:'symbols',symbols:'600519.SH',version_source:'active'}).release_id,'active');
assert.equal(backtestSpec({start:'2026-10-22',end:'2026-10-24',top_k:'10',initial_cash:'1000000',mode:'historical'}).release_id,'active');
assert.equal(backtestSpec({...base,mode:'research_replay',version_source:'active'}).release_id,base.release_id);
assert.throws(()=>backtestSpec({...base,mode:'watchlist_replay',scope:'symbols',symbols:''}),/股票/);
assert.throws(()=>backtestSpec({...base,mode:'watchlist_replay',scope:'training',symbols:''}),/股票池/);
// Existing model/rolling identity contract is preserved.
const fixed={start:'2026-10-22',end:'2026-10-24',top_k:'10',initial_cash:'1000000',mode:'frozen',model_id:'model-fixed'};
assert.equal(backtestSpec(fixed).model_id,'model-fixed');assert(!('release_id' in backtestSpec(fixed)));
assert.equal(backtestSpec({...fixed,mode:'rolling',series_id:'series-one'}).series_id,'series-one');
""")


def test_backtest_preview_body_is_read_only_and_bounded():
    run_module(r"""
import assert from 'node:assert/strict';
import {backtestPreviewBody} from './pages/backtest.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){return this.values[key]??null;}};
const base={start:'2026-10-22',end:'2026-10-24',release_id:'release-b43d198c',scope:'symbols',symbols:'002436.SZ,600862.SH'};
const body=backtestPreviewBody({...base,mode:'frozen',universe_scope:'explicit_symbols'});
assert.deepEqual(body,{mode:'frozen',release_id:'release-b43d198c',expected_release_id:'release-b43d198c',scope:'symbols',symbols:['002436.SZ','600862.SH'],start:'2026-10-22',end:'2026-10-24',universe_scope:'explicit_symbols'});
assert.equal(backtestPreviewBody({...base,mode:'frozen'}).release_id,'release-b43d198c');
assert.equal(backtestPreviewBody({...base,mode:'frozen'}).universe_scope,'release');
assert.equal(backtestPreviewBody({...base,mode:'watchlist_replay',universe_scope:'explicit_symbols'}).mode,'watchlist_replay');
const active=backtestPreviewBody({...base,mode:'frozen',release_id:''});
assert.equal(active.release_id,'active');assert.equal(active.expected_release_id,'');
assert.throws(()=>backtestPreviewBody({...base,mode:'frozen',symbols:''}),/股票/);
assert.throws(()=>backtestPreviewBody({...base,mode:'frozen',scope:'training',symbols:''}),/股票池/);
assert.throws(()=>backtestPreviewBody({...base,mode:'research_replay'}),/固定模型/);
assert.throws(()=>backtestPreviewBody({...base,mode:'frozen',symbols:Array.from({length:201},(_,index)=>`${String(index+1).padStart(6,'0')}.SZ`).join(',')}),/最多 200/);
assert.deepEqual(backtestPreviewBody({...base,mode:'frozen',scope:'all',symbols:'ignored'}).symbols,[]);
""")


def test_error_advice_maps_backend_failures_to_actions():
    run_module(r"""
import assert from 'node:assert/strict';
import {errorAdvice} from './core/ui.js';
const cutoff=errorAdvice('Fixed-model backtest must begin after all training and validation information');
assert(cutoff.includes('开始日改为'),cutoff);assert(cutoff.includes('历史回放'),cutoff);
assert(errorAdvice('开始日期必须晚于信息截止 2026-10-21').includes('2026-10-22'));
assert(errorAdvice('开始日期必须晚于信息截止',{cutoff:'2026-10-21'}).includes('2026-10-22'));
const pool=errorAdvice('请填写股票代码，例如 600519.SH。');
assert(pool.includes('关注页'),pool);assert(pool.includes('全选'),pool);
const cap=errorAdvice('有效股票池超过上限 1000');
assert(cap.includes('explicit_symbols'),cap);assert(cap.includes('有效池'),cap);
const productCap=errorAdvice('本次最多 200 只股票，已填写 300 只。请缩小范围。');
assert(productCap.includes('200'),productCap);
const series=errorAdvice('watchlist_replay 区间必须落在原序列区间内');
assert(series.includes('原序列区间'),series);
assert.equal(errorAdvice(''),'');assert.equal(errorAdvice(null),'');
assert.equal(errorAdvice('某个未知的中间层错误'),'');
""")


def test_quarantine_confirmation_body_and_api_routes():
    run_module(r"""
import assert from 'node:assert/strict';
import {quarantineConfirmBody} from './pages/tasks.js';
import {api} from './core/api.js';
assert.deepEqual(quarantineConfirmBody('qplan-abc',['backtest-one','backtest-one',' backtest-two ','']),{plan_id:'qplan-abc',ids:['backtest-one','backtest-two'],confirm:true});
assert.deepEqual(quarantineConfirmBody('qplan-abc',[]),{plan_id:'qplan-abc',ids:[],confirm:true});
const calls=[];
globalThis.fetch=async(url,options)=>{calls.push({url,options});return{ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.backtestPreview({mode:'frozen',release_id:'release-x',scope:'symbols',symbols:['600519.SH'],start:'2026-10-22',end:'2026-10-24',universe_scope:'explicit_symbols'});
await api.backtestPlan('backtest-abc');
await api.quarantinePreview();
await api.quarantineConfirm({plan_id:'qplan-abc',ids:['backtest-one'],confirm:true});
assert.equal(calls[0].url,'/api/v1/backtest/preview');
assert.equal(calls[0].options.method,'POST');
assert.deepEqual(JSON.parse(calls[0].options.body),{mode:'frozen',release_id:'release-x',scope:'symbols',symbols:['600519.SH'],start:'2026-10-22',end:'2026-10-24',universe_scope:'explicit_symbols'});
assert.equal(calls[1].url,'/api/v1/backtests/backtest-abc/plan');
assert.equal(calls[1].options.method,'POST');
assert.deepEqual(JSON.parse(calls[1].options.body),{confirm:true});
assert.equal(calls[2].url,'/api/v1/learning/workflows/quarantine');
assert.equal(calls[2].options.method,undefined);
assert.equal(calls[3].url,'/api/v1/learning/workflows/quarantine/confirm');
assert.equal(calls[3].options.method,'POST');
assert.deepEqual(JSON.parse(calls[3].options.body),{plan_id:'qplan-abc',ids:['backtest-one'],confirm:true});
assert(calls.every(call=>call.options.credentials==='same-origin'));
""")


def test_job_errors_show_the_shared_advice():
    run_module(r"""
import assert from 'node:assert/strict';
import {adviceHtml,jobSummary} from './core/jobs.js';
const html=jobSummary({id:'job-one',kind:'backtest',status:'failed',error:'Fixed-model backtest must begin after all training and validation information'});
assert(html.includes('data-job-advice'),html);
assert(html.includes('建议'),html);assert(html.includes('开始日改为'),html);assert(html.includes('历史回放'),html);
const clean=jobSummary({id:'job-two',kind:'backtest',status:'succeeded'});
assert(!clean.includes('data-job-advice'));
assert.equal(adviceHtml(''),'');
assert(adviceHtml('有效股票池超过上限 1000').includes('explicit_symbols'));
""")


def test_scan_spec_keeps_concrete_release_and_explicit_universe():
    run_module(r"""
import assert from 'node:assert/strict';
import {scanSpec} from './pages/scan.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){return this.values[key]??null;}};
const fields={end:'2026-10-24',scope:'symbols',symbols:'002436.SZ,600862.SH',version_source:'active',scan_mode:'scan',release_id:'release-b43d198c',universe_scope:'explicit_symbols'};
let result=scanSpec(fields,{activeRelease:{release_id:'release-other'}});
assert.equal(result.mode,'scan');assert.equal(result.spec.release_id,'release-b43d198c');
assert.equal(result.spec.universe_scope,'explicit_symbols');assert.deepEqual(result.spec.symbols,['002436.SZ','600862.SH']);
result=scanSpec({...fields,release_id:'',universe_scope:''},{activeRelease:{release_id:'release-other'}});
assert.equal(result.spec.release_id,'active');assert(!('universe_scope' in result.spec));
result=scanSpec({...fields,version_source:'frozen',model_id:'model-fixed',scan_mode:'scan'},{activeRelease:null});
assert.equal(result.spec.model_id,'model-fixed');assert(!('release_id' in result.spec));
assert.equal(result.spec.universe_scope,'explicit_symbols');
assert.throws(()=>scanSpec({...fields,version_source:'frozen',model_id:'',scan_mode:'scan'}),/冻结模型/);
assert.throws(()=>scanSpec({...fields,version_source:'frozen',model_id:'model-fixed',scan_mode:'kronos_filter'}),/Kronos/);
assert.throws(()=>scanSpec({...fields,version_source:'active',release_id:''},{activeRelease:null}),/默认方法/);
""")


def test_replay_range_guard_and_series_boundary_advice():
    run_module(r"""
import assert from 'node:assert/strict';
import {replayRangeError} from './pages/backtest.js';
import {errorAdvice} from './core/ui.js';
const range={start:'2022-07-05',end:'2026-10-22'};
assert.equal(replayRangeError({mode:'watchlist_replay',start:'2022-07-05',end:'2026-10-22'},range),'');
const late=replayRangeError({mode:'watchlist_replay',start:'2022-07-05',end:'2026-10-23'},range);
assert(late.includes('2026-10-23')&&late.includes('2026-10-22'),late);
const early=replayRangeError({mode:'watchlist_replay',start:'2022-07-04',end:'2026-10-22'},range);
assert(early.includes('2022-07-05'),early);
assert.equal(replayRangeError({mode:'frozen',start:'2020-01-01',end:'2030-01-01'},range),'');
assert.equal(replayRangeError({mode:'watchlist_replay',start:'2022-07-05',end:'2026-10-23'},null),'');
const advice=errorAdvice('Watchlist replay dates must lie within the saved out-of-sample series (2022-07-05..2026-10-22); requested 2022-07-05..2026-10-23');
assert(advice.includes('原序列'),advice);assert(advice.includes('结束日期'),advice);
assert(advice.includes('固定模型')&&advice.includes('十年研究'),advice);
""")


def test_forward_summary_card_reports_evidence_and_boundaries():
    run_module(r"""
import assert from 'node:assert/strict';
import {forwardSummaryBody} from './pages/models.js';
const empty=forwardSummaryBody({days:{total:0}});
assert(empty.includes('尚无前瞻记录'),empty);
const value={schema:'forward_summary_v1',days:{total:2,recorded:1,verified:1,partial:0,live:1,backfill:1},
  latest:{day:'2026-10-21',n:5,requested:5,coverage:1,rank_ic:-0.5,hit_rate:0.4,sign_agree:0.5,
          topk_overlap:1,rank_agree:0.8,mean_ret:-0.02,mean_ret_net_2x:-0.028,revision:0},
  rolling:{last5:{days:1,rank_ic_mean:-0.5,sign_agree_mean:0.5,coverage_mean:1},
           buckets:{A:{count:1,mean_ret:-0.02,hit_rate:0},B:{count:0},C:{count:1,mean_ret:-0.03,hit_rate:0.5},D:{count:0}}},
  kronos:{succeeded:2,unusable:3,requested:5,coverage:0.4},
  sample:{verified_days:1,required_days:40,status:'insufficient'},
  gates:[{gate:'agreement',status:'fail',detail:{reason:'agreement_thresholds_not_met'}}],
  limitations:['research only']};
const html=forwardSummaryBody(value);
assert(html.includes('2026-10-21')&&html.includes('-0.5')&&html.includes('40'),html);
assert(html.includes('Kronos 可用 2 / 5'),html);
assert(html.includes('互证分桶'),html);
assert(html.includes('互证闸门'),html);
assert(html.includes('回填 1'),html);
assert(html.includes('research only'),html);
""")


def test_replay_series_hint_names_newer_verified_session_and_alternatives():
    run_module(r"""
import assert from 'node:assert/strict';
import {latestVerifiedSession,replaySeriesHint} from './pages/backtest.js';
const range={start:'2022-07-05',end:'2026-10-22'};
// Only the saved-series replay gets the boundary explanation.
assert.equal(replaySeriesHint({mode:'frozen'},range,'2026-10-23'),'');
assert.equal(replaySeriesHint({mode:'rolling'},range,'2026-10-23'),'');
assert.equal(replaySeriesHint({mode:'watchlist_replay'},null,'2026-10-23'),'');
assert.equal(replaySeriesHint({mode:'watchlist_replay'},{},'2026-10-23'),'');
// Without a coverage answer the note still explains the boundary and both alternatives.
const generic=replaySeriesHint({mode:'watchlist_replay'},range);
assert(generic.includes('2022-07-05')&&generic.includes('2026-10-22'),generic);
assert(generic.includes('固定模型')&&generic.includes('十年研究'),generic);
assert(!generic.includes('2026-10-23'),generic);
// A newer verified session is named instead of only a raw range error.
const newer=replaySeriesHint({mode:'watchlist_replay'},range,'2026-10-23');
assert(newer.includes('2026-10-23')&&newer.includes('2026-10-22'),newer);
assert(newer.includes('固定模型')&&newer.includes('十年研究'),newer);
assert(newer.includes('请求截止设为 2026-10-23'),newer);
const same=replaySeriesHint({mode:'watchlist_replay'},range,'2026-10-22');
assert(!same.includes('行情已核验到'),same);
// Coverage payload forms the page may receive.
assert.equal(latestVerifiedSession({target_date:'2026-10-23',datasets:[{name:'daily_bars',coverage_status:'complete',complete_through:'2026-10-23',watermark:'2026-10-23'}]}),'2026-10-23');
assert.equal(latestVerifiedSession({target_date:'2026-10-24',datasets:[{name:'daily_bars',complete_through:'2026-10-23'}]}),'2026-10-23');
assert.equal(latestVerifiedSession({datasets:{daily_bars:{watermark:'2026-10-22'}}}),'2026-10-22');
assert.equal(latestVerifiedSession({target_date:'2026-10-23'}),'2026-10-23');
assert.equal(latestVerifiedSession({}),'');
assert.equal(latestVerifiedSession(null),'');
""")
