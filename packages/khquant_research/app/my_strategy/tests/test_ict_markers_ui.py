"""ICT liquidity events: layer toggle, period projection and popup evidence."""
import shutil
import subprocess
from pathlib import Path

import pytest


STATIC = Path(__file__).resolve().parents[1] / "web_dashboard/static/js"

HARNESS = r'''
const assert=require('node:assert/strict'),path=require('path'),fs=require('fs'),vm=require('vm');
global.KHQuantMarkers=require(path.join(process.argv[1],'chart_markers.js'));
global.KHQuantWyckoffMarkers=require(path.join(process.argv[1],'wyckoff_markers.js'));
global.KHQuantIctMarkers=require(path.join(process.argv[1],'ict_markers.js'));
global.KHQuantChartEvidence=require(path.join(process.argv[1],'chart_evidence.js'));
global.KHQuantAnalysisMethodView=require(path.join(process.argv[1],'analysis_method_view.js'));
const elements=new Map();
function newElement(tag='div') {
  const el={tag,dataset:{},style:{},checked:false,hidden:false,disabled:false,value:'',textContent:'',className:'',
    clientWidth:640,clientHeight:450,offsetWidth:460,offsetHeight:500,scrollTop:0,options:[],listeners:{},attributes:{},children:[],details:[],
    getBoundingClientRect:()=>({left:20,top:180}),addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);},
    setAttribute(key,value){this.attributes[key]=value;},hasAttribute(key){return key in this.attributes;},
    removeAttribute(key){delete this.attributes[key];},append(node){this.children.push(node);},
    replaceChildren(){this.children=[];this.innerHTML='';},
    querySelectorAll(selector){return selector==='details[data-agent-details]' ? this.details : [];},
    querySelector(selector){const key=selector.match(/^details\[data-agent-details="([^"]+)"\]$/)?.[1];
      return key ? this.details.find(node=>node.dataset.agentDetails===key) : newElement();}};
  let html='';
  Object.defineProperty(el,'innerHTML',{configurable:true,get(){return html;},set(value){html=String(value);this.scrollTop=0;
    this.details=[...html.matchAll(/<details\b[^>]*data-agent-details="([^"]+)"[^>]*>/g)].map(match=>({
      dataset:{agentDetails:match[1]},open:/\bopen\b/.test(match[0])}));}});
  return el;
}
function element(id){if(!elements.has(id))elements.set(id,newElement());return elements.get(id);}
global.document={getElementById:element,documentElement:{},querySelectorAll:()=>[],
  createElementNS:(_,tag)=>newElement(tag),addEventListener(){}};
global.window={innerWidth:977,innerHeight:904,addEventListener(){}};
global.location={hash:'analysis'};global.sessionStorage={getItem:()=>null};
global.getComputedStyle=()=>({getPropertyValue:()=> '#123456'});
global.ResizeObserver=class{observe(){}};global.requestAnimationFrame=()=>1;
global.setTimeout=()=>1;global.clearTimeout=()=>{};
let click,candles;
const scale={subscribeVisibleLogicalRangeChange(){},getVisibleLogicalRange:()=>({from:0,to:1}),setVisibleLogicalRange(){},
  fitContent(){},height:()=>30,timeToCoordinate:()=>50};
function series(){return {markers:[],setData(){},setMarkers(rows){this.markers=rows;},applyOptions(){},priceScale:()=>({applyOptions(){}}),priceToCoordinate:price=>350-price};}
const chart={addCandlestickSeries:()=>(candles=series()),addHistogramSeries:series,addLineSeries:series,removeSeries(){},resize(){},
  subscribeCrosshairMove(){},subscribeClick(fn){click=fn;},timeScale:()=>scale,priceScale:()=>({width:()=>50})};
global.LightweightCharts={createChart:()=>chart,LineStyle:{Solid:0,Dashed:2}};window.LightweightCharts=LightweightCharts;
vm.runInThisContext(fs.readFileSync(path.join(process.argv[1],'workbench.js'),'utf8')
  .replace('init().catch(showError);',
    'globalThis.workbench={state,bind,renderAnalysis,renderChartPopup};'));
workbench.bind();
const dates=['2026-07-01','2026-07-02','2026-07-03'];
const dailyBars=dates.map(time=>({time,open:10,high:12,low:9,close:11,volume:100,is_closed:true}));
const sweep={symbol:'002436.SZ',event_type:'ict_sweep',event_id:'ict-sweep-1',side:'BUY',confirmed:false,
  origin_observed_at:dates[0]+'T15:00:00+08:00',first_available_at:dates[0]+'T15:00:00+08:00',
  ict_sweep:{kind:'sellside_sweep',extreme:9,reference:{price:9.5,time:'2026-06-28'},session:'daily'},
  support_low:9,invalidation_price:9,reason_codes:[]};
const setup={symbol:'002436.SZ',event_type:'ict_setup',event_id:'ict-setup-1',side:'BUY',confirmed:true,origin_event_id:'ict-sweep-1',
  origin_observed_at:dates[0]+'T15:00:00+08:00',first_available_at:dates[1]+'T15:00:00+08:00',
  ict_entry:{zone:[9.5,10],ce:9.75,retest_day:dates[1]},ict_gross_rr:2.6,ict_minimum_rr:2,
  ict_target:{price:13,time:'2026-06-20'},ict_dealing_range:{low:8.5,high:11,equilibrium:9.75},
  ict_mss:{reference:{price:10.5,time:'2026-06-30'},break_day:dates[0]},invalidation_price:9,reason_codes:[]};
const broken={symbol:'002436.SZ',event_type:'ict_setup_invalidated',event_id:'ict-broken-1',side:'SELL',confirmed:true,
  invalidates_event_id:'ict-setup-1',origin_observed_at:dates[1]+'T15:00:00+08:00',
  first_available_at:dates[2]+'T15:00:00+08:00',invalidation_price:9,reason_codes:[]};
function load(frequency,bars,events) {
  workbench.state.frequency=frequency;
  workbench.state.analysis={symbol:'002436.SZ',as_of:dates[2],data_end:dates[2],ict_events:events,
    frequencies:{'日线':{bars:dailyBars,indicators:{rows:dailyBars}},'周线':{bars,indicators:{rows:bars}}},
    chart_evidence:{rows:dates.map(date=>({date,reference_close:11,agents:{}}))}};
  workbench.state.query={symbol:'002436.SZ',start:dates[0],end:dates[2]};
}
'''


def run_js(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for shipped ICT marker checks")
    result = subprocess.run([node, "-e", HARNESS + "\n(async()=>{\n" + script +
                             "\n})().catch(error=>{console.error(error);process.exitCode=1;});", str(STATIC)],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_ict_layer_toggle_shows_and_hides_causal_liquidity_markers():
    run_js(r'''
load('日线',dailyBars,[sweep,setup,broken]);
element('view-analysis').hidden=false;element('layer-ict').checked=true;
workbench.renderAnalysis(false);
assert.deepEqual(candles.markers.map(({time,position,color,shape})=>[time,position,color,shape]),[
  [dates[0],'belowBar','#0f766e','circle'],
  [dates[1],'belowBar','#0f766e','circle'],
  [dates[2],'aboveBar','#6d28d9','square']]);
element('layer-ict').checked=false;workbench.renderAnalysis(false);
assert.deepEqual(candles.markers,[],'unchecking the ICT layer removes every liquidity marker');
''')


def test_ict_events_project_onto_forming_weekly_period_with_daily_identity():
    run_js(r'''
const week=[{time:dates[2],open:10,high:12,low:9,close:11,volume:300,is_closed:false}];
load('周线',week,[sweep,setup]);
element('view-analysis').hidden=false;element('layer-ict').checked=true;
workbench.renderAnalysis(false);
assert.deepEqual(candles.markers.map(({time,position,color,shape,text})=>[time,position,color,shape,text]),
  [[dates[2],'belowBar','#0f766e','circle','2']],'both daily events project onto their week with the count');
click({time:dates[2],point:{x:350,y:180}});
const popup=element('chart-evidence-popup');assert(!popup.hidden);
assert(popup.innerHTML.includes('日线事件已可知／周期形成中'),'projected groups keep the daily evidence clock');
assert(popup.innerHTML.includes('本根K线 ICT 流动性事件 2 条'));
assert(popup.innerHTML.includes('ICT 流动性清扫 · 买方 · 待确认'));
assert(popup.innerHTML.includes('ICT 严格 ICT 确认 · 买方 · 已确认'));
''')


def test_ict_popup_lists_confirmed_setup_with_frozen_levels_and_reason_labels():
    run_js(r'''
load('日线',dailyBars,[sweep,setup]);
element('view-analysis').hidden=false;element('layer-ict').checked=true;
workbench.renderAnalysis(false);click({time:dates[1],point:{x:350,y:180}});
let html=element('chart-evidence-popup').innerHTML;
assert(html.includes('ICT ict_setup确认'),'single confirmed setup keeps a compact badge');
assert(html.includes('本根K线 ICT 流动性事件 1 条'));
assert(html.includes('ICT 严格 ICT 确认 · 买方 · 已确认'));
assert(html.includes('入场区 9.5 – 10 · CE 9.75'));
assert(html.includes('毛 R:R 2.6（门槛 2，未计费用）'));
assert(html.includes('候选目标流动性 13（2026-06-20 摆动）'));
assert(html.includes('交易区间 8.5 – 11 · 均衡 9.75'));
assert(html.includes('原冻结失效边界 9 元'));
assert(html.includes('首次可用 '+dates[1]));
click({time:dates[0],point:{x:350,y:180}});
html=element('chart-evidence-popup').innerHTML;
assert(html.includes('ICT 流动性清扫 · 买方 · 待确认'));
assert(html.includes('清扫极值 9 · 参考摆动 9.5（2026-06-28）'));
const brokenDay={...broken,reason_codes:['close_broke_frozen_ict_boundary','later_event_confirmed']};
load('日线',dailyBars,[brokenDay]);workbench.renderAnalysis(false);click({time:dates[2],point:{x:350,y:180}});
html=element('chart-evidence-popup').innerHTML;
assert(html.includes('ICT 结构失效 · 卖方 · 已确认'));
assert(html.includes('收盘价破坏该 ICT 事件的原失效边界；后续 ICT 事件已确认，原待确认事件结束'));
''')


def test_ict_statistics_and_event_list_label():
    run_js(r'''
const evidence=KHQuantChartEvidence;
const html=evidence.statisticsHtml({visible_window:{bars:2,source_events:1,wyckoff_events:0,ict_events:3,final_points:0}});
assert(html.includes('ICT 事件 3'),'statistics expose the ICT event count');
assert(html.includes('日线 2'));
''')
