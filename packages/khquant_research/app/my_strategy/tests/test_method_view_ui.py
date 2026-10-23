"""Read-only method plots, cutoff markers and saved scan navigation, with synthetic inputs."""
import shutil
import subprocess

import pytest

from test_czsc_display_dates_ui import HARNESS, STATIC

SETUP = HARNESS.replace('applyResearchContext};', 'applyResearchContext,prepareMethodLayers,renderEvents,methodEvidenceResult,invalidateMethodPresentation,loadScanPage,renderScan,showResult};') + r'''
chart.remove=()=>{};scale.setVisibleRange=()=>{};ResizeObserver.prototype.disconnect=()=>{};
const memory=new Map();global.sessionStorage={getItem:key=>memory.get(key)||null,setItem:(key,value)=>memory.set(key,String(value))};
vm.runInThisContext(fs.readFileSync(path.join(process.argv[1],'prediction_chart.js'),'utf8'));
vm.runInThisContext(fs.readFileSync(path.join(process.argv[1],'method_presentation.js'),'utf8'));
const method='structure_kronos_ma_mlp',cutoff='2001-01-02',context={analysis_method:method,score_model_id:'synthetic-model',lookback:128,horizon:5,samples:32,seed:7,device:'cpu',budget_seconds:300,max_predictions:200};
const view={schema:'method_display_v1',method,label:'合成均线方法',as_of:cutoff,status:'complete',valid:true,direction:'BUY',forecast_valid:true,prediction_ref:'synthetic-ref',model:{model_id:'synthetic-model'},method_reference_points:[{symbol:'000001.SZ',method,reference_layer:'method',date:cutoff,time:cutoff,action:'BUY',reference_price:10,market_cutoff_at:cutoff+'T15:00:00+08:00'}]};
const result={analysis_mode:'reference',analysis_method:method,symbol:'000001.SZ',data_end:cutoff,as_of:cutoff,method_view:view,method_context:context};
const counts={expected:5000,matched:5000,complete:4600,buy:1200,sell:1300,wait:2100,unavailable:300,failed:50,deferred:50};
const board={board_id:'synthetic-board',name:'合成行业',kind:'industry',counts:{...counts,expected:100},forecast:{count:90,mean_member_return_median:.02,mean_member_up_path_fraction:.6},score:{count:85,mean:55},coverage:{method_complete_ratio:.8}};
const summary={identity:{summary_sha256:'synthetic-membership-sha'},selection:{run_id:'synthetic-run',method,as_of:cutoff},method_context:context,population_summary:{counts},boards:[board]};
const readCalls=[];let readAnswer=async url=>url.includes('/members?')?{...summary,board,rows:[{symbol:'000001.SZ',method_view:view}],pagination:{total:1,page_size:40}}:url.includes('/synthetic-run?')?summary:{selection:{method,as_of:cutoff},runs:[{run_id:'synthetic-run',context,status:'complete'}]};
global.fetch=async(url,options={})=>{readCalls.push({url,options});const value=await readAnswer(url);return {ok:true,status:200,json:async()=>value,text:async()=>JSON.stringify(value)};};
vm.runInThisContext(fs.readFileSync(path.join(process.argv[1],'board_method_view.js'),'utf8').replace('globalThis.KHQuantBoardMethods={onRoute,refresh:', 'globalThis.boardMethods={state,load,chooseRun,openBoard,loadMembers,draft,prediction};globalThis.KHQuantBoardMethods={onRoute,refresh:'));
KHQuantMethods.adoptScanContext(context,cutoff);
'''


def run(body):
    node=shutil.which('node')
    if not node:
        pytest.skip('Node is required for UI contract tests')
    output=subprocess.run([node,'-e',SETUP+'\n(async()=>{'+body+'\n})().catch(error=>{console.error(error);process.exitCode=1;});',str(STATIC)],capture_output=True,text=True)
    assert output.returncode==0,output.stderr


def test_method_markers_are_cutoff_only_and_never_alias_source_history():
    run(r'''
const inputs=KHQuantChartEvidence.methodMarkerInputs(result);assert.equal(inputs.length,1);assert.equal(inputs[0].date,cutoff);assert.equal(inputs[0].first_available_at,undefined);
for(const replacement of [{valid:false},{status:'unavailable'},{direction:'WAIT'},{as_of:'2000-12-29'},{method:'structure_kronos'}])assert.equal(KHQuantChartEvidence.methodMarkerInputs({...result,method_view:{...view,...replacement}}).length,0);
const earlier={...view.method_reference_points[0],date:'2000-12-29'};assert.equal(KHQuantChartEvidence.methodMarkerInputs({...result,method_view:{...view,method_reference_points:[earlier]}}).length,0);
const built=KHQuantMarkers.build({bars:[{time:cutoff}],dailyBars:[{time:cutoff}],events:inputs,symbol:result.symbol,frequency:'日线',asOf:cutoff});
assert.equal(built.markers.length,1);assert.equal(built.markers[0].text,'法B');assert.equal(built.groups[0].kind,'method_reference');assert.equal(built.mappings[0].first_available_at,null);
assert.equal(KHQuantMethodPresentation.category({method_view:{...view,valid:false,status:'unavailable',direction:'WAIT'}}),'unavailable');
assert.equal(KHQuantMethodPresentation.label({status:'not_in_scan'}),'未入扫描');
''')


def test_prediction_instances_are_independent_and_clear_cancels_stale_response():
    run(r'''
let finish;const make=(ref,symbol)=>({symbol,as_of:cutoff,result_ref:{cache_key:ref},forecast:[]});
const first=KHQuantPredictionChart.create({prefix:'analysis',isVisible:()=>false}),second=KHQuantPredictionChart.create({prefix:'board',isVisible:()=>false});
const pending=first.render(make('one','000001.SZ'),()=>new Promise(resolve=>finish=resolve));
await second.render(make('two','000002.SZ'),async()=>({symbol:'000002.SZ',as_of:cutoff,prediction_ref:'two',history:[],history_verification:{status:'unavailable',reason:'第二实例'},forecast:[]}));
first.clear('已切换方法');finish({symbol:'000001.SZ',as_of:cutoff,prediction_ref:'one',history:[],history_verification:{status:'unavailable',reason:'陈旧实例'},forecast:[]});await pending;
assert.equal(element('analysis-prediction-history-state').textContent,'已切换方法');assert(element('board-prediction-history-state').textContent.includes('第二实例'));
assert.equal(readCalls.length,0);
''')


def test_analysis_low_quality_still_uses_original_ref_and_invalidates_on_edit():
    run(r'''
let renders=[];KHQuantPredictionChart.create=()=>({render:(value,api)=>renders.push(value),clear(){},onRoute(){}});
KHQuantMethodPresentation.analysisChart({...result,method_view:{...view,valid:false,forecast_valid:false,status:'unavailable'}},()=>{throw Error('unexpected');});
assert.equal(renders[0].result_ref.cache_key,'synthetic-ref');assert(element('analysis-prediction-summary').textContent.includes('方法不可用'));assert(element('analysis-prediction-summary').textContent.includes('预测路径质量不足'));assert.equal(KHQuantChartEvidence.methodMarkerInputs({...result,method_view:{...view,valid:false,forecast_valid:false,status:'unavailable'}}).length,0);
KHQuantMethodPresentation.analysisChart(result,()=>{}, {dirty:true});assert(element('analysis-prediction').hidden);
workbench.prepareMethodLayers(result);assert(!element('layer-sources').checked,'有效组合方法结果默认隐藏来源层');
workbench.prepareMethodLayers({analysis_method:'structure'});assert(element('layer-sources').checked,'纯结构结果显示来源层');
''')


def test_board_candidates_require_explicit_selection_and_frozen_context_drives_draft():
    run(r'''
await boardMethods.load({method,as_of:cutoff,method_context:context});assert.equal(readCalls.length,1);assert.equal(boardMethods.state.result,null);
await boardMethods.chooseRun('synthetic-run');assert.equal(boardMethods.state.result,summary);
assert(element('board-method-context').textContent.includes('32 条路径'));assert(element('board-method-summary').innerHTML.includes('5,000'));
await boardMethods.openBoard(board.board_id);assert(element('board-method-members').innerHTML.includes('带入分析'));
element('analysis-start').value='1999-01-01';boardMethods.draft({symbol:'000001.SZ'});
assert.equal(element('analysis-symbol').value,'000001.SZ');assert.equal(element('analysis-end').value,cutoff);
assert.equal(element('analysis-start').value,'','保存上下文没有起始日时草稿不臆造');
assert.equal(element('analysis-score-model').value,'','方法固定为结构与量价，不再带入评分模型');
assert.equal(location.hash,'analysis');assert(readCalls.every(call=>!call.options.method||call.options.method==='GET'));
'''.replace('assert.equal(lastAnalysisQuery,undefined);','assert.equal(globalThis.lastAnalysisQuery,undefined);'))


def test_board_rejects_wrong_identity_and_older_response_after_method_switch():
    run(r'''
await boardMethods.load({method,as_of:cutoff,method_context:context});let finish;readAnswer=async()=>new Promise(resolve=>finish=resolve);
const pending=boardMethods.chooseRun('synthetic-run');await settle();
readAnswer=async()=>({selection:{method:'structure_kronos',as_of:cutoff},runs:[]});
await boardMethods.load({method:'structure_kronos',as_of:cutoff,method_context:{analysis_method:'structure_kronos'}});
finish(summary);await pending;assert.equal(boardMethods.state.result,null);assert.equal(boardMethods.state.selection.method,'structure_kronos');
readAnswer=async()=>({...summary,selection:{...summary.selection,as_of:'2000-12-29'}});await boardMethods.chooseRun('synthetic-run');assert.equal(boardMethods.state.result,null);assert(element('board-method-state').textContent.includes('不一致'));
''')


def test_scan_saved_match_checks_actual_parameters_and_new_full_population_filter():
    run(r'''
element('scan-start').value='2000-01-01';element('scan-end').value=cutoff;
workbench.state.scan={analysis_method:'structure',data_end:cutoff,as_of:cutoff,run_id:'structure-run',method_context:{analysis_method:'structure',start:'2000-01-01'},rows:[],coverage:{requested:5000},scan_view:{paged:true,total:5000,total_rows:5000}};
assert.equal(KHQuantMethods.scanSelection().run_id,'structure-run');
element('scan-start').value='1999-01-01';assert.equal(KHQuantMethods.scanSelection().run_id,null,'草稿日期与保存结果不一致时不隐含选择');
element('scan-start').value='2000-01-01';assert.equal(KHQuantMethods.scanSelection().run_id,'structure-run');
workbench.state.scan={...result,run_id:'synthetic-run',rows:[],coverage:{requested:5000},scan_view:{paged:true,total:5000,total_rows:5000}};
assert.equal(KHQuantMethods.scanSelection().run_id,null,'固定结构方法下保存的预测方法扫描不与当前草稿匹配，仅只读浏览');
element('scan-filter').value='watch';readAnswer=async url=>{assert(url.includes('/api/boards/method-scans/synthetic-run/stocks?'));assert(url.includes('category=wait'));return {...workbench.state.scan,population_summary:{counts},rows:[{symbol:'000001.SZ',method_view:{...view,direction:'WAIT'},method_evaluation:{method,status:'complete'}}],scan_view:{paged:true,total:2100,total_rows:5000}};};
await workbench.loadScanPage();assert(element('scan-coverage').textContent.includes('5,000'));assert.equal(element('scan-count').textContent,'2,100');assert.equal(workbench.state.scan.scan_view.total_rows,5000);assert.equal(workbench.state.scan.population_summary.counts.expected,5000);assert(element('scan-page-label').textContent.includes('2,100'));
''')


def test_membership_change_is_rejected_without_replacing_selected_snapshot():
    run(r'''
await boardMethods.load({method,as_of:cutoff,method_context:context});await boardMethods.chooseRun('synthetic-run');
readAnswer=async()=>({...summary,identity:{summary_sha256:'changed-membership'},board,rows:[],pagination:{total:0,page_size:40}});
await boardMethods.openBoard(board.board_id);assert.equal(boardMethods.state.members,null);assert(element('board-method-member-state').textContent.includes('快照已变化'));assert.equal(boardMethods.state.result.identity.summary_sha256,'synthetic-membership-sha');
''')


def test_method_popup_keeps_selected_source_evidence_separate():
    run(r'''
const methodInput=KHQuantChartEvidence.methodMarkerInputs(result)[0];
const source={symbol:result.symbol,action:'BUY',source:'structure',source_label:'结构',reference_layer:'source',reference_price:10,reference_price_date:cutoff,date:cutoff,evidence_date:cutoff,reference_only:true,first_available_at:cutoff+'T15:00:00+08:00',confirmed:true};
const built=KHQuantMarkers.build({bars:[{time:cutoff}],dailyBars:[{time:cutoff}],events:[methodInput,source],symbol:result.symbol,frequency:'日线',asOf:cutoff});
const html=KHQuantChartEvidence.popupHtml({evidence:{rows:[]},groups:built.groups,barDate:cutoff,frequency:'日线'});
assert(html.includes('截止日方向参考'));assert(html.includes('同日来源与其他已选证据'));assert(html.includes('结构'));
''')


def test_method_event_list_tracks_enabled_layers_without_relabeling_source_history():
    run(r'''
const source={symbol:result.symbol,reference_layer:'source',source:'structure',source_label:'结构',action:'SELL',reference_only:true,confirmed:true,reference_price:9,reference_price_date:'2000-12-29',date:'2000-12-29',first_available_at:'2000-12-29T15:00:00+08:00'};
workbench.state.analysis={...result,source_reference_points:[source],frequencies:{'日线':{bars:[{time:cutoff,open:1,high:1,low:1,close:1,volume:100,is_closed:true}],indicators:{rows:[]}}},chart_evidence:{rows:[]}};
workbench.state.query={symbol:result.symbol,end:cutoff};element('view-analysis').hidden=false;workbench.prepareMethodLayers(result);
workbench.renderEvents();assert(element('event-list').innerHTML.includes('合成均线方法'));assert(!element('event-list').innerHTML.includes('结构来源'));assert.equal(element('event-locate-s').disabled,true);
workbench.invalidateMethodPresentation();assert(!element('event-list').innerHTML.includes('合成均线方法'),'草稿参数变化后方法层失效，不再显示方法事件');
element('layer-sources').checked=true;workbench.renderEvents();assert(element('event-list').innerHTML.includes('结构来源'));assert(!element('event-list').innerHTML.includes('合成均线方法'));assert.equal(element('event-locate-s').disabled,false);
''')


def test_saved_null_model_and_zero_seed_replace_stale_drafts_explicitly():
    run(r'''
element('scan-start').value='1999-01-01';element('analysis-start').value='1999-01-01';
KHQuantMethods.adoptScanContext({...context,start:'2000-01-01'},cutoff);
assert.equal(element('scan-start').value,'2000-01-01');assert.equal(element('scan-end').value,cutoff);
KHQuantMethods.draftAnalysis('000001.SZ',cutoff,{...context,score_model_id:null,seed:0,start:'2000-01-01'},'synthetic-no-model-run');
const actual=KHQuantMethods.analysisContext();
assert.equal(actual.score_model_id,undefined);assert.equal(actual.seed,undefined,'固定方法草稿不再携带评分模型与种子');
assert.equal(actual.start,'2000-01-01');assert.equal(actual.end,cutoff);assert.equal(actual.analysis_method,'legacy_reference');
assert.equal(element('analysis-symbol').value,'000001.SZ');assert.equal(element('analysis-start').value,'2000-01-01');assert.equal(readCalls.length,0);
KHQuantMethods.draftAnalysis('000001.SZ',cutoff,{...context,start:null,device:null},'synthetic-no-start-run');assert.equal(element('analysis-start').value,'','起始日期为空时不臆造');
''')


def test_hidden_board_summary_cannot_overwrite_new_stock_draft_and_return_can_retry():
    run(r'''
await boardMethods.load({method,as_of:cutoff,method_context:context});let finish;const previous=readAnswer;readAnswer=async()=>new Promise(resolve=>finish=resolve);
const pending=boardMethods.chooseRun('synthetic-run');await settle();KHQuantBoardMethods.onRoute(false);element('scan-start').value='1999-01-01';
finish(summary);await pending;assert.equal(element('scan-start').value,'1999-01-01','隐藏面板的迟到响应不改写当前扫描草稿');assert.equal(boardMethods.state.result,null);
readAnswer=previous;KHQuantBoardMethods.onRoute(true);await settle();
assert(element('board-method-state').textContent.includes('结构与量价'),'固定结构方法下板块方法面板提示查看原生来源');
assert.equal(readCalls.length,2,'固定方法下路由不再发起方法扫描请求');
await boardMethods.load({method,as_of:cutoff,method_context:context});await boardMethods.chooseRun('synthetic-run');
assert.equal(element('scan-end').value,cutoff);assert.equal(boardMethods.state.result,summary,'显式重试仍可读取保存证据');
''')


def test_saved_scan_start_and_usage_changes_do_not_implicitly_select_old_run():
    run(r'''
const saved={analysis_method:'structure',start:'2000-01-01',usage_mode:'historical'};
element('scan-start').value='2000-01-01';element('scan-end').value=cutoff;
workbench.state.scan={analysis_method:'structure',data_end:cutoff,as_of:cutoff,run_id:'structure-run',method_context:saved,rows:[]};
assert.equal(KHQuantMethods.scanSelection().run_id,'structure-run');
element('scan-start').value='1999-01-01';assert.equal(KHQuantMethods.scanSelection().run_id,null);assert.equal(KHQuantMethods.scanSelection().method_context.start,'1999-01-01');
KHQuantMethods.adoptScanContext(saved,cutoff);assert.equal(KHQuantMethods.scanSelection().run_id,'structure-run','恢复相同日期后重新匹配保存结果');
KHQuantMethods.adoptScanContext({...saved,start:null},cutoff);workbench.state.scan.method_context={...saved,start:''};assert.equal(KHQuantMethods.scanSelection().run_id,'structure-run');
element('scan-end').value='2000-12-29';assert.equal(KHQuantMethods.scanSelection().run_id,null,'截止日变化后不隐含选择旧运行');
''')
