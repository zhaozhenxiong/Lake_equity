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


FIXTURE = r"""
const bar={time:'2026-10-23',open:10,high:11,low:9,close:10.5,volume:1000};
const analysis={lake_run_id:'analysis-one',symbol:'000001.SZ',as_of:'2026-10-23',data_end:'2026-10-23',input_manifest_id:'manifest-one',price_basis:'unadjusted',frequencies:{'日线':{bars:[bar]},'周线':{bars:[bar]}}};
const result={symbol:'000001.SZ',as_of:'2026-10-23',quality:{prediction_usable:true},identity:{parameters:{horizon:1}},forecast:[{date:'2026-10-26',close:11,close_p10:10,close_p90:12}],analysis_binding:{compatible:true,analysis_run_id:'analysis-one',symbol:'000001.SZ',as_of:'2026-10-23',input_manifest_id:'manifest-one',price_basis:'unadjusted',lookback:1,input_window_sha256:'a'.repeat(64),history_input_sha256:'b'.repeat(64)}};
const chart={symbol:result.symbol,as_of:result.as_of,forecast:result.forecast,history:[{...bar,date:bar.time}],history_verification:{status:'verified',input_sha256_match:true},input:{price_basis:'unadjusted',data_sha256:'b'.repeat(64)},parameters:{horizon:1}};
"""


def test_analysis_overlay_requires_matching_saved_context_and_input_window():
    run_module(r"""
import assert from 'node:assert/strict';
import {analysisPredictionCompatible} from './core/prediction.js';
""" + FIXTURE + r"""
assert.equal(analysisPredictionCompatible(analysis,result,chart).compatible,true);
for(const [change,value] of [['symbol','600519.SH'],['as_of','2026-10-22'],['input_manifest_id','new-manifest'],['price_basis','qfq']]){
assert.equal(analysisPredictionCompatible({...analysis,[change]:value},result,chart).compatible,false,change);
}
assert.equal(analysisPredictionCompatible(analysis,{...result,analysis_binding:null},chart).compatible,false);
assert.equal(analysisPredictionCompatible(analysis,result,{...chart,input:{...chart.input,data_sha256:'c'.repeat(64)}}).compatible,false);
assert.equal(analysisPredictionCompatible(analysis,result,{...chart,history:[{...bar,date:bar.time,close:10.6}]}).compatible,false);
assert.equal(analysisPredictionCompatible(analysis,result,{...chart,history_verification:{status:'unavailable'}}).compatible,false);
assert.equal(analysisPredictionCompatible(analysis,result,{...chart,forecast:[{...chart.forecast[0],date:'2026-10-23'}]}).compatible,false);
assert.equal(analysisPredictionCompatible(analysis,result,{...chart,forecast:[{...chart.forecast[0],close_p90:9}]}).compatible,false);
""")


def test_prediction_quality_is_separate_from_execution_status_and_calibration():
    run_module(r"""
import assert from 'node:assert/strict';
import {predictionQuality,predictionReference} from './core/prediction.js';
""" + FIXTURE + r"""
assert.equal(predictionQuality(result,{chart}).state,'usable');
assert.equal(predictionQuality({...result,status:'completed',quality:{prediction_usable:false}},{chart}).state,'degraded');
assert.equal(predictionQuality({...result,quality:{}},{chart}).state,'unavailable');
assert.equal(predictionQuality({...result,forecast:[]},{chart:{...chart,forecast:[]}}).state,'unavailable');
assert.equal(predictionQuality(result,{chart:{...chart,history_verification:{status:'unavailable'}}}).state,'degraded');
assert.equal(predictionQuality(result,{chart:{...chart,history_verification:{...chart.history_verification,display_only_degraded:true}}}).state,'degraded');
assert.equal(predictionQuality(result,{error:{status:404,message:'expired'}}).state,'unavailable');
assert.equal(predictionQuality(result,{error:{status:409,message:'corrupt'}}).state,'failed');
assert.equal(predictionReference({result_ref:{cache_key:'saved-key'}}),'saved-key');
""")


def test_analysis_request_is_bound_to_current_result_not_unsubmitted_form_values():
    run_module(r"""
import assert from 'node:assert/strict';
import {analysisPredictionSpec,analysisInputsMatch} from './pages/analysis.js';
""" + FIXTURE + r"""
globalThis.FormData=class{constructor(form){this.form=form;}get(key){return this.form[key]??null;}};
const form={symbol:'000001.SZ',start:'2026-01-01',end:'2026-10-23'};
const spec=analysisPredictionSpec(analysis,1);
assert.equal(spec.analysis_run_id,'analysis-one');assert.equal(spec.end,'2026-10-23');assert.equal(spec.symbol,'000001.SZ');assert.equal(spec.horizon,1);
assert.equal(analysisInputsMatch(analysis,form),true);assert.equal(analysisInputsMatch(analysis,{...form,symbol:'600519.SH'}),false);assert.equal(analysisInputsMatch(analysis,{...form,end:'2026-10-26'}),false);
assert.throws(()=>analysisPredictionSpec(analysis,10),/1.*5/);
assert.throws(()=>analysisPredictionSpec({...analysis,input_manifest_id:null},1),/身份/);
""")


def test_main_structure_chart_adds_and_hides_forecast_without_changing_real_bars():
    run_module(r"""
import assert from 'node:assert/strict';
import {createResearchChart} from './core/charts.js';
""" + FIXTURE + r"""
const nodes=[];
function element(){const value={clientWidth:1000,clientHeight:500,style:{},children:[],attrs:{},append(child){this.children.push(child);},replaceChildren(){this.children=[];},setAttribute(key,value){this.attrs[key]=value;},addEventListener(){},removeEventListener(){}};nodes.push(value);return value;}
globalThis.document={createElement:element,createElementNS:element};globalThis.cancelAnimationFrame=()=>{};globalThis.requestAnimationFrame=callback=>{callback();return 1;};globalThis.ResizeObserver=class{observe(){}disconnect(){}};
const series=[];
const add=kind=>{const value={kind,rows:[],setData(rows){this.rows=rows;},priceToCoordinate:value=>400-value*20,priceScale:()=>({applyOptions(){}}),applyOptions(){},setMarkers(){}};series.push(value);return value;};
const times={timeToCoordinate:date=>date==='2026-10-23'?700:720,subscribeVisibleLogicalRangeChange(){},fitContent(){}};
globalThis.LightweightCharts={createChart:()=>({addCandlestickSeries:()=>add('candle'),addHistogramSeries:()=>add('volume'),addLineSeries:()=>add('line'),removeSeries:line=>{line.removed=true;},timeScale:()=>times,subscribeCrosshairMove(){},subscribeClick(){},remove(){},resize(){}})};
const original=JSON.stringify(analysis.frequencies),host=element(),view=createResearchChart(host);
view.render(analysis,{prediction:{result,chart}});
assert.equal(series.find(row=>row.kind==='candle').rows.length,1);assert.equal(series.filter(row=>row.kind==='line'&&!row.removed).length,3);
assert.equal(series.find(row=>row.kind==='line').rows.at(-1).time,'2026-10-26');
assert(nodes.some(row=>row.attrs['data-forecast-band']==='p10-p90'));assert(nodes.some(row=>row.attrs['data-forecast-cutoff']==='2026-10-23'));
view.render(analysis,{frequency:'周线',prediction:{result,chart}});assert.equal(series.filter(row=>row.kind==='line'&&!row.removed).length,0);
assert.equal(JSON.stringify(analysis.frequencies),original);view.destroy();
""")
