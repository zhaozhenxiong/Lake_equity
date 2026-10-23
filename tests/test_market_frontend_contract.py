from pathlib import Path
import shutil
import subprocess

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def run_module(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for frontend behavior checks")
    result = subprocess.run([node, "--input-type=module", "-e", source], cwd=FRONTEND,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


DOM = r"""
import assert from 'node:assert/strict';
import {mount} from './pages/market.js';
const observers=[];
globalThis.ResizeObserver=class{constructor(callback){this.callback=callback;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
function element(dataset={}){return {dataset,value:'',innerHTML:'',textContent:'button',isConnected:true,hidden:false,disabled:false,clientWidth:800,clientHeight:430,listeners:new Map(),attributes:new Map(),addEventListener(kind,callback){this.listeners.set(kind,callback);},removeEventListener(){},setAttribute(key,value){this.attributes.set(key,value);},querySelectorAll(){return [];},focus(){}};}
const nodes=new Map();
const tabs=['boards','breadth','capital','specialties'].map(value=>element({marketTab:value}));
const windows=['20','60'].map(value=>element({marketWindow:value}));
const host={...element(),querySelector(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);},querySelectorAll(selector){return selector==='[data-market-tab]'?tabs:selector==='[data-market-window]'?windows:[];}};
for(const [selector,value] of Object.entries({'[data-market-kind]':'all','[data-market-sort]':'amount','[data-market-color]':'amount_change_pct','[data-market-trend-metric]':'amount','[name=basis]':'members','[name=end]':'2026-10-22','[data-market-section]':'breadth'}))host.querySelector(selector).value=value;
host.querySelector('[data-market-prepare]').disabled=true;
const calls=[];
const response=body=>({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>body});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
"""


def test_navigation_and_module_boundaries():
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert app.index("id:'data'") < app.index("id:'market'") < app.index("id:'analysis'")
    page = (FRONTEND / "pages" / "market.js").read_text(encoding="utf-8")
    assert "<iframe" not in page
    assert "api.submit" not in page
    assert "api.prepareMarket" in page
    assert "后端尚未载入，请等当前任务结束后切换服务" in page
    assert "marketWindow" in page and "60交易日" in page
    for path in ("app.js", "core/api.js", "core/market-charts.js", "pages/market.js"):
        result = subprocess.run([shutil.which("node"), "--check", str(FRONTEND / path)],
                                capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr


def test_api_pins_views_and_encodes_opaque_board_identity():
    run_module(r"""
import assert from 'node:assert/strict';
import {api} from './core/api.js';
const calls=[];
globalThis.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({})};};
await api.marketStatus();
await api.prepareMarket({basis:'members',end:'2026-10-22'});
await api.marketFeatures({view_id:'frozen/a',kind:'mixed',sort_by:'amount_change',limit:30});
await api.marketHistory('ths:881001/industry',{view_id:'frozen/a',window:60});
await api.marketMembers('ths:881001/industry',{view_id:'frozen/a',offset:100,limit:100});
await api.marketSpecialties({view_id:'frozen/a',section:'valuation'});
assert.equal(calls[0].url,'/api/v1/market/status');
assert.equal(calls[1].options.method,'POST');
assert.deepEqual(JSON.parse(calls[1].options.body),{basis:'members',end:'2026-10-22'});
assert(calls[2].url.includes('view_id=frozen%2Fa&kind=mixed&sort_by=amount_change&limit=30'));
assert.equal(calls[3].url,'/api/v1/market/boards/ths%3A881001%2Findustry/history?view_id=frozen%2Fa&window=60');
assert(calls[4].url.endsWith('members?view_id=frozen%2Fa&offset=100&limit=100'));
assert(calls[5].url.endsWith('specialties?view_id=frozen%2Fa&section=valuation'));
assert(calls.every(call=>call.options.credentials==='same-origin'));
""")


def test_amount_units_and_real_missing_values():
    run_module(r"""
import assert from 'node:assert/strict';
import {amountUnit,marketAmount,signedPercent,trendValues} from './core/market-charts.js';
assert.equal(marketAmount(200000000,{amount:'CNY'}),'2');
assert.equal(marketAmount(200000000,{amount:'source_native'}),'200,000,000');
assert.equal(amountUnit({amount:'source_native'}),'原始单位');
assert.equal(marketAmount(null,{amount:'CNY'}),'—');
assert.equal(marketAmount(0,{amount:'CNY'}),'0');
assert.equal(signedPercent(.1),'+10%');
assert.equal(signedPercent(null),'—');
const points=[{date:'2026-10-20',amount:100000000,market_share:.1},{date:'2026-10-21',amount:null,market_share:null},{date:'2026-10-22',amount:0,market_share:0}];
assert.deepEqual(trendValues(points,'amount',{amount:'CNY'}).map(row=>row.value),[1,null,0]);
assert.deepEqual(trendValues(points,'market_share').map(row=>row.value),[10,null,0]);
assert.throws(()=>trendValues([...points].reverse(),'amount'),/顺序/);
assert.throws(()=>trendValues([points[0],points[0]],'amount'),/重复/);
""")


def test_treemap_area_is_actual_amount_and_does_not_mutate_input():
    run_module(r"""
import assert from 'node:assert/strict';
import {treemapLayout} from './core/market-charts.js';
const rows=[{board_id:'a',amount:300},{board_id:'b',amount:100},{board_id:'missing',amount:null},{board_id:'zero',amount:0}];
const before=JSON.stringify(rows),boxes=treemapLayout(rows,1000,400);
assert.equal(boxes.length,2);
assert.equal(boxes[0].width*boxes[0].height,300000);
assert.equal(boxes[1].width*boxes[1].height,100000);
assert(boxes.every(box=>box.left>=0&&box.top>=0&&box.width>0&&box.height>0));
assert.equal(JSON.stringify(rows),before);
assert.deepEqual(treemapLayout([{amount:null}]),[]);
""")


def test_real_svg_chart_gaps_keyboard_and_disposal():
    run_module(r"""
import assert from 'node:assert/strict';
import {createMarketTreemap,createMarketTrend} from './core/market-charts.js';
const observers=[];
globalThis.ResizeObserver=class{constructor(callback){this.callback=callback;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
const host={clientWidth:800,clientHeight:430,innerHTML:'',listeners:new Map(),addEventListener(key,callback){this.listeners.set(key,callback);},removeEventListener(key){this.listeners.delete(key);},querySelectorAll(){return [];}};
let selected;
const row={board_id:'real:1/2',name:'<script>source</script>',amount:100,amount_change_pct:null};
const map=createMarketTreemap(host,[row],{units:{amount:'source_native'},onSelect:value=>selected=value});
assert(host.innerHTML.includes('<svg'));
assert(host.innerHTML.includes('&lt;script&gt;'));
assert(!host.innerHTML.includes('<script>'));
assert(host.innerHTML.includes('role="button" tabindex="0"'));
assert(host.innerHTML.includes('原始单位'));
host.listeners.get('keydown')({type:'keydown',key:'Enter',preventDefault(){},target:{closest:()=>({dataset:{marketBoard:row.board_id}})}});
assert.equal(selected,row);
map.destroy();assert.equal(host.listeners.size,0);
const trend=createMarketTrend(host,[{date:'2026-10-20',relative_amount20:1},{date:'2026-10-21',relative_amount20:null},{date:'2026-10-22',relative_amount20:2}],{metric:'relative_amount20'});
assert(host.innerHTML.includes('class="market-gap"'));
assert(!host.innerHTML.includes('<polyline'));
assert.equal((host.innerHTML.match(/<circle/g)||[]).length,2);
trend.destroy();assert(observers.every(observer=>observer.disconnected));
""")


def test_specialty_keeps_full_population_summary_separate_from_preview():
    run_module(r"""
import assert from 'node:assert/strict';
import {specialtyPanel,marketWarnings} from './pages/market.js';
const html=specialtyPanel({id:'valuation',title:'估值',dataset:'valuation_metrics',available:true,columns:[{key:'symbol',label:'代码'},{key:'amount',label:'金额',unit:'source_native'}],rows:[{symbol:'<img src=x>',amount:null}],summary:{population_count:5224,metrics:{pe_ttm:{positive_count:4000,missing_count:1224,positive_median:20}}},units:{amount:'source_native'},total_count:5224,truncated:true,notes:['非正值不伪造为零']});
assert(html.includes('5224'));
assert(html.includes('4,000'));
assert(html.includes('完整人口指标'));
assert(html.includes('原始预览已截断'));
assert(html.includes('完整匹配人口聚合'));
assert(html.includes('source_native'));
assert(!html.includes('亿元'));
assert(html.includes('&lt;img src=x&gt;'));
assert(marketWarnings([{reason:'<unsafe>'}]).includes('&lt;unsafe&gt;'));
const absent=specialtyPanel({id:'flows',title:'资金',dataset:'fund_flow',available:false,status:'unavailable',rows:[],summary:{},notes:['无物理发布版本']});
assert(absent.includes('无物理发布版本'));
assert(absent.includes('尚无可读的已发布数据'));
""")


def test_board_twelve_observations_are_not_stock_twenty_feature_inputs():
    run_module(r"""
import assert from 'node:assert/strict';
import {boardFeaturePanel} from './pages/market.js';
const html=boardFeaturePanel({board_return_1:null,return_1:.5,board_amount:null,amount:100000000,board_return_20:-.1},{amount:'source_native'});
assert.equal((html.match(/data-market-feature=/g)||[]).length,12);
assert(html.includes('data-market-feature="board_return_1">—</dd>'));
assert(html.includes('data-market-feature="board_amount">—</dd>'));
assert(html.includes('-10%'));
assert(html.includes('原始单位'));
assert(!html.includes('亿元'));
assert(html.includes('不是 ma_trend_v1 的 20 项股票评分输入'));
assert(html.includes('不赋予训练资格'));
""")


def test_mount_without_view_never_prepares_or_runs_jobs():
    run_module(DOM + r"""
globalThis.fetch=async(url,options)=>{calls.push({url,options});return response({latest_view:null,capabilities:{sector_bars:{available:false}}});};
const controller=new AbortController();
const cleanup=await mount(host,{signal:controller.signal});
assert.deepEqual(calls.map(call=>call.url),['/api/v1/market/status']);
assert.equal(nodes.get('[name=basis]').value,'members');
assert.equal(nodes.get('[data-market-prepare]').disabled,false);
assert(nodes.get('[data-market-map]').innerHTML.includes('尚未生成行情视图'));
for(const [selector,label] of Object.entries({'[data-market-kind]':'类型','[data-market-sort]':'展示排序','[data-market-color]':'方块颜色','[data-market-trend-metric]':'走势指标'}))assert.equal(nodes.get(selector).attributes.get('aria-label'),label);
cleanup();
""")


def test_backend_not_loaded_is_not_reported_as_missing_lake_data():
    run_module(DOM + r"""
globalThis.fetch=async()=>({ok:false,status:404,headers:{get:()=> 'application/json'},json:async()=>({detail:'Not Found'})});
const cleanup=await mount(host,{signal:new AbortController().signal});
assert(nodes.get('[data-market-error]').innerHTML.includes('后端尚未载入，请等当前任务结束后切换服务'));
assert(nodes.get('[data-market-view-status]').textContent.includes('不代表数据湖没有数据'));
assert.equal(nodes.get('[data-market-prepare]').disabled,true);
cleanup();
""")


def test_saved_view_reads_frozen_detail_and_true_sixty_session_window():
    run_module(DOM + r"""
const view={view_id:'frozen-one',basis:'members',price_date:'2026-10-22'};
const board={board_id:'em:710/industry',name:'真实板块',kind:'industry',amount:200000000,amount_change:10000000,amount_change_pct:.05,return_1:.01,coverage:{available:10,expected:11,comparable:9}};
globalThis.fetch=async(url,options)=>{calls.push({url,options});if(url.endsWith('/status'))return response({latest_view:view});if(url.includes('/features?'))return response({...view,units:{amount:'CNY'},summary:{market_amount:1000000000,available_boards:1,total_boards:1},boards:[board],top_gainers:[board],top_losers:[],training_eligible:false});if(url.includes('/history?'))return response({points:[{date:'2026-10-22',amount:200000000}],units:{amount:'CNY'}});if(url.includes('/members?'))return response({rows:[{symbol:'000001.SZ',name:'平安',close:10,amount:100000000}],status:'available',total_count:1});throw Error(url);};
const cleanup=await mount(host,{signal:new AbortController().signal});
assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
assert(calls.some(call=>call.url.includes('em%3A710%2Findustry/history?view_id=frozen-one&window=20')));
assert(nodes.get('[data-market-map]').innerHTML.includes('<svg'));
assert(nodes.get('[data-market-movers]').innerHTML.includes('真实板块'));
assert(nodes.get('[data-market-history-notes]').innerHTML.includes('<summary>走势口径与来源说明</summary>'));
assert(!nodes.get('[data-market-history-notes]').innerHTML.includes('<details class="json-disclosure" open'));
windows[1].listeners.get('click')();await flush();
assert(calls.some(call=>call.url.endsWith('history?view_id=frozen-one&window=60')));
assert(nodes.get('[data-market-history-caption]').textContent.includes('请求 60 交易日，实际返回 1 点'));
nodes.get('[data-market-members-toggle]').listeners.get('click')();await flush();
assert(calls.some(call=>call.url.endsWith('members?view_id=frozen-one&offset=0&limit=100')));
assert(nodes.get('[data-market-members-content]').innerHTML.includes('#analysis?symbol=000001.SZ&amp;end=2026-10-22'));
cleanup();assert(observers.every(observer=>observer.disconnected));
""")


def test_source_basis_keeps_board_units_separate_from_market_denominator():
    run_module(DOM + r"""
const view={view_id:'source-view',basis:'source',price_date:'2026-10-22'};
globalThis.fetch=async(url)=>{if(url.endsWith('/status'))return response({latest_view:view});if(url.includes('/features?'))return response({...view,units:{amount:'source_native'},summary:{market_amount:900000000,units:{amount:'CNY'},available_boards:0,total_boards:427},boards:[],top_gainers:[],top_losers:[],training_eligible:false});throw Error(url);};
const cleanup=await mount(host,{signal:new AbortController().signal});
assert(nodes.get('[data-market-metrics]').innerHTML.includes('本地市场成交额（亿元）'));
assert(nodes.get('[data-market-metrics]').innerHTML.includes('>9<'));
assert(nodes.get('[data-market-basis-label]').textContent.includes('原始单位'));
cleanup();
""")


def test_prepare_is_explicit_and_failed_new_view_never_keeps_old_board_chart():
    run_module(DOM + r"""
const old={view_id:'old-view',basis:'members',price_date:'2026-10-22'};
const next={view_id:'new-view',basis:'source',price_date:'2026-10-22'};
const board={board_id:'old-board',name:'旧板块',amount:100};
globalThis.FormData=class{get(key){return key==='basis'?'source':'2026-10-22';}};
globalThis.fetch=async(url,options)=>{calls.push({url,options});if(url.endsWith('/status'))return response({latest_view:old});if(url.endsWith('/prepare'))return response(next);if(url.includes('/features?view_id=old-view'))return response({...old,units:{amount:'CNY'},summary:{},boards:[board]});if(url.includes('/history?'))return response({points:[{date:'2026-10-22',amount:100}]});if(url.includes('/features?view_id=new-view'))return {ok:false,status:400,headers:{get:()=> 'application/json'},json:async()=>({detail:'synthetic new-view read failed'})};throw Error(url);};
const cleanup=await mount(host,{signal:new AbortController().signal});
assert.equal(calls.filter(call=>call.url.endsWith('/prepare')).length,0);
assert(nodes.get('[data-market-map]').innerHTML.includes('旧板块'));
await nodes.get('[data-market-prepare-form]').listeners.get('submit')({preventDefault(){},currentTarget:{}});
const post=calls.find(call=>call.url.endsWith('/prepare'));
assert.deepEqual(JSON.parse(post.options.body),{basis:'source',end:'2026-10-22'});
assert.equal(calls.filter(call=>call.url.endsWith('/prepare')).length,1);
assert(!nodes.get('[data-market-map]').innerHTML.includes('旧板块'));
assert(nodes.get('[data-market-error]').innerHTML.includes('synthetic new-view read failed'));
assert.equal(nodes.get('[data-market-members-toggle]').disabled,true);
cleanup();
""")


def test_mismatched_history_identity_is_rejected_not_stitched():
    run_module(DOM + r"""
const view={view_id:'expected-view',read_ref:'expected-ref',basis:'members',price_date:'2026-10-22'};
globalThis.fetch=async(url)=>{if(url.endsWith('/status'))return response({latest_view:view});if(url.includes('/features?'))return response({...view,units:{amount:'CNY'},summary:{},boards:[{board_id:'real-board',amount:100,name:'真实板块'}]});if(url.includes('/history?'))return response({view_id:'another-view',board_id:'real-board',points:[{date:'2026-10-22',amount:100}]});throw Error(url);};
const cleanup=await mount(host,{signal:new AbortController().signal});
assert(nodes.get('[data-market-trend]').innerHTML.includes('走势读取身份与冻结板块不一致'));
assert(!nodes.get('[data-market-trend]').innerHTML.includes('<svg'));
cleanup();
""")


def test_specialty_tabs_read_same_frozen_reference_without_reprepare():
    run_module(DOM + r"""
const view={view_id:'frozen-view',read_ref:'frozen-ref',basis:'members',price_date:'2026-10-22'};
globalThis.fetch=async(url,options)=>{calls.push({url,options});if(url.endsWith('/status'))return response({latest_view:view});if(url.includes('/features?'))return response({...view,summary:{},units:{amount:'CNY'},boards:[]});if(url.includes('/specialties?'))return response({read_ref:'frozen-ref',section:'breadth',panels:[{id:'breadth',title:'市场广度',dataset:'market_breadth',available:true,rows:[{metric_id:'advance_ratio',value:.5}],summary:{population_count:7},total_count:7,truncated:true}]});throw Error(url);};
const cleanup=await mount(host,{signal:new AbortController().signal});
tabs[1].listeners.get('click')();await flush();
assert(calls.some(call=>call.url.endsWith('specialties?view_id=frozen-view&section=breadth')));
assert(nodes.get('[data-market-specialty-content]').innerHTML.includes('市场广度'));
tabs[0].listeners.get('click')();tabs[1].listeners.get('click')();await flush();
assert.equal(calls.filter(call=>call.url.includes('/specialties?')).length,1);
assert.equal(calls.filter(call=>call.options.method==='POST').length,0);
cleanup();
""")
