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


def test_selection_full_precision_sort_is_stable_and_preserves_frozen_rank():
    run_module(r"""
import assert from 'node:assert/strict';
import {selectionPage} from './core/selection.js';
const rows=[{symbol:'B',score:.023446191,rank:4,status:'scored'},{symbol:'A',score:.023451068,rank:3,status:'scored'},{symbol:'D',score:null,status:'missing'},{symbol:'C',score:-.1,rank:5,status:'scored'},{symbol:'E',score:.023451068,rank:2,status:'scored'}];
const before=JSON.stringify(rows);
assert.deepEqual(selectionPage(rows,{sort:'desc'}).rows.map(r=>r.symbol),['A','E','B','C','D']);
assert.deepEqual(selectionPage(rows,{sort:'asc'}).rows.map(r=>r.symbol),['C','B','A','E','D']);
const page=selectionPage(rows,{sort:'desc',pageSize:2,page:2});
assert.equal(page.rows[0].rank,4);assert.equal(page.total,5);assert.equal(page.received,5);
assert.equal(JSON.stringify(rows),before);
assert.equal(selectionPage(rows,{status:'missing',sort:'asc'}).rows[0].score,null);
""")


def test_selection_filters_full_population_and_does_not_invent_sell():
    run_module(r"""
import assert from 'node:assert/strict';
import {selectionPage,decisionView} from './core/selection.js';
const rows=Array.from({length:5239},(_,i)=>({symbol:String(i),score:i,status:i<984?'scored':i<1000?'missing':'excluded',decision:{action:i<10?'buy_candidate':'watch',label:i<10?'买入候选':'观察'}}));
const view=selectionPage(rows,{status:'scored',suggestion:'buy_candidate',sort:'desc',pageSize:3,page:2});
assert.equal(view.total,10);assert.equal(view.received,5239);assert.equal(view.rows[0].symbol,'6');
assert.equal(selectionPage(rows,{query:'5238'}).rows[0].symbol,'5238');
assert.equal(decisionView({score:-.2,status:'scored'}).label,'未生成研究建议');
assert.equal(decisionView({score:1,status:'missing'}).action,'unknown');
""")


def test_saved_source_links_never_replace_identity_with_active():
    run_module(r"""
import assert from 'node:assert/strict';
import {selectionLinks,selectionSource,sourceCompatible} from './core/selection.js';
const report={run_id:'scan-old',family:'model_scan_v1',model_id:'model-old',release_id:'release-old',read_ref:'read-old',as_of:'2026-10-23',scope:'symbols'};
const links=selectionLinks(report,{symbol:'600519.SH'});
assert(links.backtest.includes('release_id=release-old'));assert(!links.backtest.includes('active'));
assert(links.trading.includes('source_id=scan-old'));assert(links.trading.includes('source_type=model_scan_v1'));
assert(sourceCompatible(report,{source:selectionSource(report)}));
for(const key of ['run_id','model_id','release_id','read_ref','as_of'])assert(!sourceCompatible(report,{source:{...selectionSource(report),[key]:'wrong'}}),key);
assert(!selectionLinks({...report,release_id:null},{}).backtest.includes('active'));
assert(selectionLinks({...report,release_id:null},{symbol:'600519.SH'}).backtest.includes('model_id=model-old'));
""")


def test_pure_factor_kronos_and_missing_name_are_explicit_and_escaped():
    run_module(r"""
import assert from 'node:assert/strict';
import {selectionRowsHtml} from './core/selection-view.js';
const html=selectionRowsHtml({run_id:'scan',family:'model_scan_v1',as_of:'2026-10-23'},[{symbol:'600519.SH',name:'<unsafe>',score:-.01,status:'scored'},{symbol:'000001.SZ',score:null,status:'missing'}]);
assert(html.includes('本次未执行'));assert(html.includes('名称缺失'));assert(html.includes('&lt;unsafe&gt;'));assert(!html.includes('<unsafe>'));
assert(html.includes('selection-cards'));assert(html.includes('data-selection-evidence'));assert(html.includes('未生成研究建议'));
assert(!html.includes('卖出候选'));
""")


def test_explanation_drawer_renders_actual_signed_contributions_and_checks_source():
    run_module(r"""
import assert from 'node:assert/strict';
import {explanationCompatible} from './core/selection.js';
import {explanationHtml} from './core/selection-view.js';
const report={run_id:'scan',model_id:'m',release_id:'r',read_ref:'g',as_of:'2026-10-23'};
const explanation={status:'saved',capability:'exact_additive',source:{...report,symbol:'600519.SH'},score:.01,intercept:.03,reconstructed_score:.01,reconstruction_error:0,features:[{name:'ma5_bias',raw_value:1,imputed:false,coefficient:-.02,standardized_value:1,contribution:-.02}]};
assert(explanationCompatible(report,'600519.SH',explanation));
assert(!explanationCompatible(report,'000001.SZ',explanation));
assert(!explanationCompatible({...report,read_ref:'new'},'600519.SH',explanation));
assert(!explanationCompatible({...report,rows:[{symbol:'600519.SH',score:1}]},'600519.SH',explanation));
const html=explanationHtml(explanation);
assert(html.includes('5日均线偏离'));assert(html.includes('-0.02'));assert(html.includes('截距'));assert(html.includes('重算误差'));
assert(explanationHtml({status:'not_saved'}).includes('原报告未保存指标解释'));
assert(explanationHtml({status:'unsupported'}).includes('暂不提供逐指标贡献'));
assert(!explanationHtml({status:'not_saved',reason:'row_was_not_scored'}).includes('data-selection-build-explanation'));
""")


def test_decision_wrapper_keeps_original_forward_qualification():
    run_module(r"""
import assert from 'node:assert/strict';
import {selectionOverviewHtml} from './core/selection-view.js';
const html=selectionOverviewHtml({qualification:{research_only:true,independent_forward_status:'pending'},population:{}},{qualification:{research_only:true,original:{independent_forward_status:'pending'}},rows:[]});
assert(html.includes('待验证'));assert(html.includes('未授予'));
""")


def test_scan_default_omits_today_and_preserves_explicit_dates():
    run_module(r"""
import assert from 'node:assert/strict';
import {scanSpec} from './pages/scan.js';
globalThis.FormData=class{constructor(values){this.values=values;}get(key){return this.values[key]??null;}};
const fields={scope:'all',end:'',version_source:'active',scan_mode:'scan',release_id:'release-saved'};
assert(!('end' in scanSpec(fields,{activeRelease:{release_id:'release-active'}}).spec));
assert.equal(scanSpec({...fields,end:'2026-10-23'},{activeRelease:{}}).spec.end,'2026-10-23');
""")
