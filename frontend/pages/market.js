import {api} from '../core/api.js';
import {badge,busy,disclosure,empty,errorHtml,escapeHtml,heading,number,percent,routeLink,today,table} from '../core/ui.js';
import {amountUnit,createMarketTreemap,createMarketTrend,finite,marketAmount,signedPercent} from '../core/market-charts.js';

const tabNames={boards:'板块成交',breadth:'指数与广度',capital:'资金与估值',specialties:'CNEquity 专项'};
const kindNames={all:'全部类型',industry:'行业',concept:'概念',mixed:'混合/未细分'};
const sectionNames={breadth:'指数与广度',flows:'资金流',valuation:'估值',industry:'行业',derivatives:'衍生品',macro:'宏观',intraday:'分钟'};
const fieldNames={as_of:'实际观测日期',coverage_start:'覆盖开始',coverage_end:'覆盖截至',population_count:'完整人口行数',unique_symbols:'实际股票数',industry_count:'行业数',priced_industries:'有收益行业数',positive_count:'正值数量',negative_count:'负值数量',median_ret:'行业收益中位数',n_members:'成员数',n_priced:'行情成员数',n_excluded:'排除成员数',observed_metric_count:'已观测指标数',symbol_count:'品种数',indicator_count:'宏观指标数',zero_volume_count:'零成交量数',configured_scope:'配置范围',configured_watchlist:'配置关注池',level:'行业层级',weighting:'加权口径',metric:'指标',bucket:'区间',count:'数量',symbol:'代码',trade_date:'交易日',source:'来源',value:'数值',available:'已发布',rows:'行数',industry_code:'行业代码',ret:'行业收益',amount:'成交额',observed_count:'观测数量',finite_count:'有限数值数量',missing_count:'缺失数量',invalid_count:'无效数量',nonpositive_count:'非正值数量',positive_median:'正值中位数',positive_p25:'正值25%分位',positive_p75:'正值75%分位',pe_ttm:'市盈率TTM',pe_dynamic:'动态市盈率',pb:'市净率',ps_ttm:'市销率TTM',total_mv:'总市值',float_mv:'流通市值',shares_as_of:'股本日期',source_counts:'来源分布',metrics:'完整人口指标',watchlist_status:'关注池实际覆盖',total_mv_basis_counts:'总市值口径分布',float_mv_basis_counts:'流通市值口径分布',exchange_counts:'交易所分布',model_status_counts:'模型状态分布'};
const basisName=basis=>basis==='members'?'CNE 成员聚合':'THS 来源板块';
const text=value=>value===null||value===undefined?'—':typeof value==='object'?JSON.stringify(value):String(value);
const notes=value=>Array.isArray(value)?value:value?[value]:[];
const noteText=value=>typeof value==='object'?value.message || value.reason || JSON.stringify(value):String(value);
const tone=value=>!finite(value)||value===0?'muted':value>0?'up':'down';

export function boardFeaturePanel(row,units={},basis='members') {
  const fields=[['board_return_1','1日收益','return'],['board_return_5','5日收益','return'],['board_return_10','10日收益','return'],['board_return_20','20日收益','return'],['board_advance_ratio','上涨成员占比','ratio'],['board_above_ma20_ratio','站上 MA20','ratio'],['board_breakout20_ratio','20日突破成员','ratio'],['board_amount',`成交额（${amountUnit(units)}）`,'amount'],['board_market_amount_share','本地市场成交占比','ratio'],['board_relative_amount20','相对成交额20日','relative'],['board_volatility20','20日年化波动','ratio'],['board_drawdown20','20日最大回撤','ratio']];
  const aliases={board_return_1:'return_1',board_amount:'amount',board_market_amount_share:'market_share',board_relative_amount20:'relative_amount20'};
  return `<section class="market-board-features" aria-label="十二项板块观察特征"><h3>板块观察特征 <span>12 项 · 仅观察</span></h3><dl>${fields.map(([key,label,format])=>{
    const value=row[key]===undefined?row[aliases[key]]:row[key];
    const output=format==='amount'?marketAmount(value,units):format==='return'?signedPercent(value):format==='ratio'?percent(value):finite(value)?`${number(value)} 倍`:'—';
    return `<div><dt>${escapeHtml(label)}</dt><dd class="${format==='return'?tone(value):''}" data-market-feature="${key}">${output}</dd></div>`;
  }).join('')}</dl><p>${basis==='source'?'来源板块指标不等同于成员聚合；':'固定当前成员回看历史仅供展示；'}这不是 ma_trend_v1 的 20 项股票评分输入，不赋予训练资格。</p></section>`;
}

export function marketWarnings(values) {
  const items=notes(values).filter(Boolean);
  return items.length?`<div class="market-warnings" role="note">${items.map(value=>`<p>${escapeHtml(noteText(value))}</p>`).join('')}</div>`:'';
}

export function specialtyPanel(panel) {
  const rows=Array.isArray(panel.rows)?panel.rows:[];
  const columns=(Array.isArray(panel.columns)&&panel.columns.length?panel.columns:Object.keys(rows[0] || {})).map(column=>typeof column==='string'?{key:column,label:column}:column);
  const count=panel.total_count ?? rows.length;
  const summary=Object.entries(panel.summary || {});
  const units=panel.units || {};
  const simple=summary.filter(([key,value])=>key!=='revision_id'&&(value===null || typeof value!=='object'));
  const structured=summary.filter(([,value])=>value && typeof value==='object').map(([key,value])=>{
    if(Array.isArray(value)&&!value.some(item=>item&&typeof item==='object'))return `<div class="market-summary-detail"><h3>${escapeHtml(fieldNames[key] || key)}</h3><p>${escapeHtml(value.join('，'))}</p></div>`;
    const items=Array.isArray(value)?value:Object.entries(value).map(([name,item])=>item&&typeof item==='object'?{metric:fieldNames[name] || name,...item}:{metric:fieldNames[name] || name,value:item});
    const keys=[...new Set(items.flatMap(item=>Object.keys(item)))];
    return `<div class="market-summary-detail"><h3>${escapeHtml(fieldNames[key] || key)}</h3>${table(keys.map(name=>({label:fieldNames[name] || name,render:row=>escapeHtml(typeof row[name]==='number'?number(row[name],4):typeof row[name]==='boolean'?row[name]?'是':'否':text(row[name]))})),items,{emptyTitle:'尚无已观测统计',emptyDescription:'缺失不视为零。'})}</div>`;
  }).join('');
  return `<section class="panel market-specialty-panel"><div class="panel-header"><div><h2>${escapeHtml(panel.title || panel.id)}</h2><p><a href="${escapeHtml(routeLink('data',{dataset:panel.dataset}))}">${escapeHtml(panel.dataset)}</a></p></div>${badge(panel.available===true?'available':panel.status || 'unavailable',panel.available===true?'已发布':panel.status || '尚未发布')}</div>${simple.length?`<dl class="market-specialty-summary">${simple.map(([key,value])=>`<div><dt>${escapeHtml(fieldNames[key] || key)}</dt><dd>${escapeHtml(text(value))}</dd></div>`).join('')}</dl>`:''}${structured}${marketWarnings(panel.notes)}<div class="market-unit-note">单位：${escapeHtml(Object.entries(units).map(([key,value])=>`${key}=${text(value)}`).join('；') || '以列声明及来源口径为准，不自行换算')}${panel.summary?.revision_id?`<br>发布版本：${escapeHtml(panel.summary.revision_id)}`:''}</div>${rows.length?table(columns.map(column=>({label:`${fieldNames[column.label] || column.label || fieldNames[column.key] || column.key}${column.unit?`（${column.unit}）`:''}`,render:row=>escapeHtml(typeof row[column.key]==='number'?number(row[column.key],4):text(row[column.key])),wrap:column.key==='notes'})),rows):empty(panel.available===true?'当前范围没有返回记录':'尚无可读的已发布数据','不会在浏览时补采，也不会用零值或其他来源填充。')}<div class="table-footer"><span>返回 ${number(rows.length,0)} 行 / 匹配 ${number(count,0)} 行</span><span>${panel.truncated?'原始预览已截断；上方摘要由完整匹配人口聚合':'按实际返回范围展示'}</span></div></section>`;
}

export async function mount(host,{signal,params=new URLSearchParams()}) {
  let view=null,features=null,selected=null,mapChart,trendChart,history=null,capabilities={};
  let tab=Object.hasOwn(tabNames,params.get('tab'))?params.get('tab'):'boards';
  let featuresController,detailController,membersController,specialController;
  let featuresSequence=0,detailSequence=0,membersSequence=0,specialSequence=0;
  let membersOpen=false,membersOffset=0,membersTotal=0;
  const specialtyCache=new Map();
  host.innerHTML=heading('行情特征','从已发布数据观察板块成交、市场广度与来源特征；读取不补采、不训练。',`<a class="button" href="#data">查看数据湖</a>`)+`<section class="panel market-controls"><form data-market-prepare-form><label>板块数据口径<select name="basis"><option value="members">CNE 成员聚合 · 人民币成交额</option><option value="source">THS 来源板块 · 原始单位</option></select></label><label>行情截止<input name="end" type="date" value="${today()}"></label><button class="primary" type="submit" data-market-prepare disabled>生成行情视图</button><span class="market-scope-status" data-market-view-status role="status">正在读取已保存视图…</span></form><p class="form-note" data-market-scope-note>生成仅在本地读取已发布版本并冻结身份，不启动任何来源补采。</p></section><div class="market-tabs" role="tablist" aria-label="行情特征视图">${Object.entries(tabNames).map(([key,label])=>`<button type="button" role="tab" id="market-tab-${key}" data-market-tab="${key}" aria-controls="market-panel-${key}" aria-selected="${key===tab}" tabindex="${key===tab?0:-1}">${label}</button>`).join('')}</div><div data-market-error></div><div data-market-boards role="tabpanel" id="market-panel-boards" aria-labelledby="market-tab-boards"><div class="metrics market-metrics" data-market-metrics></div><section class="panel"><div class="market-board-toolbar"><div><h2>板块成交全景</h2><p data-market-basis-label>等待一份冻结的行情视图</p></div><div class="market-inline-controls"><label>类型<select data-market-kind><option value="all">全部类型</option><option value="industry">行业</option><option value="concept">概念</option><option value="mixed">混合/未细分</option></select></label><label>展示排序<select data-market-sort><option value="amount">成交额 Top30</option><option value="amount_change">成交额变化 Top30</option><option value="relative_amount">相对成交额 Top30</option><option value="activity">活跃度 Top30</option></select></label><label>方块颜色<select data-market-color><option value="amount_change_pct">成交额较前日增减</option><option value="return_1">当日涨跌幅</option></select></label></div></div><div class="market-capital-grid"><div class="market-map-wrap"><div class="market-map-legend"><span>面积＝成交额</span><span data-market-color-legend>颜色＝成交额较前日增减</span><span><i class="market-legend-up"></i>增加 <i class="market-legend-down"></i>减少 <i class="market-legend-missing"></i>缺失</span></div><div class="market-treemap" data-market-map></div><p class="chart-caption" data-market-map-caption></p></div><aside class="market-movers" aria-label="完整合格人口成交额增减前五"><div data-market-movers></div></aside></div><div data-market-warnings></div><p class="market-method-note">成交额是交易活跃度代理，不是资金净流入。板块存在重叠，不相加当作全市场资金；历史展示不授予训练资格。</p></section><section class="panel space-top" data-market-detail><div class="market-board-toolbar"><div><h2 data-market-board-title>选择一个板块</h2><p data-market-board-meta>点击热力图、增减榜或表格，读取同一冻结版本的走势。</p></div><div class="market-inline-controls"><label>走势指标<select data-market-trend-metric><option value="amount">成交额</option><option value="relative_amount20">相对成交额（20日）</option><option value="market_share">市场成交额占比</option></select></label><div class="segmented" aria-label="板块走势交易日窗口"><button type="button" data-market-window="20" aria-pressed="true">20交易日</button><button type="button" data-market-window="60" aria-pressed="false">60交易日</button></div><button class="small-button" type="button" data-market-members-toggle disabled aria-expanded="false" aria-controls="market-members">查看成员</button></div></div><div class="market-trend" data-market-trend></div><div data-market-history-notes></div><div class="chart-caption" data-market-history-caption></div><div id="market-members" class="market-members" data-market-members hidden><div class="panel-header"><h3>当前冻结口径的板块成员</h3><span data-market-members-count></span></div><div data-market-members-content></div><div class="table-footer"><span data-market-members-position></span><div class="button-row"><button class="small-button" type="button" data-market-members-prev disabled>上一页</button><button class="small-button" type="button" data-market-members-next disabled>下一页</button></div></div></div></section><section class="panel space-top"><div class="panel-header"><h2>板块排行</h2><span class="small muted" data-market-row-count></span></div><div data-market-table></div></section></div><section data-market-specialty role="tabpanel" hidden><div class="panel market-section-controls"><label data-market-section-label>数据主题<select data-market-section></select></label><span class="muted small">原始预览与完整人口聚合摘要分别标识，缺源不会伪装成零。</span></div><div class="market-specialty-grid" data-market-specialty-content></div><div data-market-specialty-receipt></div></section><section class="panel space-top market-evidence"><div class="panel-header"><h2>本次读取身份</h2><span class="badge">不具备训练资格</span></div><div data-market-receipt></div></section>`;
  const node=selector=>host.querySelector(selector);
  node('[data-market-specialty]').id='market-panel-specialty';
  for(const [selector,label] of Object.entries({'[name=basis]':'板块数据口径','[name=end]':'行情截止','[data-market-kind]':'类型','[data-market-sort]':'展示排序','[data-market-color]':'方块颜色','[data-market-trend-metric]':'走势指标','[data-market-section]':'专项数据主题'}))node(selector).setAttribute('aria-label',label);
  let windowSize=20;
  const requestSignal=controller=>AbortSignal.any([signal,controller.signal]);
  function cancelReads(){featuresController?.abort();detailController?.abort();membersController?.abort();specialController?.abort();}
  signal.addEventListener('abort',cancelReads,{once:true});
  function showPageError(error){if(error.name!=='AbortError')node('[data-market-error]').innerHTML=errorHtml(error);}
  function noView() {
    node('[data-market-metrics]').innerHTML='';
    node('[data-market-map]').innerHTML=empty(view?.view_id?'等待读取冻结视图':'尚未生成行情视图','选择数据口径和截止日，再点击“生成行情视图”。仅冻结已发布数据，不自动补采。');
    node('[data-market-movers]').innerHTML=empty('等待可比板块','增减榜使用完整合格人口，缺失不按零值排名。');
    node('[data-market-table]').innerHTML=empty('没有已读取的板块','不会用历史旧库、样例数据或其他口径自动回填。');
    node('[data-market-trend]').innerHTML=empty('选择一个板块','生成并读取行情视图后查看真实走势。');
    node('[data-market-receipt]').innerHTML=disclosure('当前来源能力',capabilities);
    node('[data-market-view-status]').textContent='尚无已保存行情视图';
    node('[data-market-board-title]').textContent='选择一个板块';
    node('[data-market-members-toggle]').disabled=true;
    node('[data-market-members-content]').innerHTML='';
    node('[data-market-history-notes]').innerHTML='';
    node('[data-market-history-caption]').textContent='';
  }
  function renderMetrics(){
    const summary=features.summary || {},units=features.units || {};
    const marketUnits=summary.units || (units.amount==='CNY'?units:{}),knownMarketUnits=summary.units || units.amount==='CNY';
    node('[data-market-metrics]').innerHTML=[['行情截止',features.price_date || '—',features.membership_date?`成员 ${features.membership_date}`:'成员日期以读取凭证为准'],[`本地市场成交额（${knownMarketUnits?amountUnit(marketUnits):'单位待核验'}）`,marketAmount(knownMarketUnits?summary.market_amount:null,marketUnits),'固定人口分母，不加总重叠板块'],['可用板块 / 全部',`${number(summary.available_boards,0)} / ${number(summary.total_boards,0)}`,'冻结视图全部类型的完整人口'],['当前图中板块',`${number(features.boards?.length ?? 0,0)} 个`,'Top5 独立取自当前类型完整人口']].map(([label,value,detail])=>`<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><div class="metric-detail">${escapeHtml(detail)}</div></div>`).join('');
  }
  function renderMovers(){
    const gainers=Array.isArray(features.top_gainers)?features.top_gainers.slice(0,5):[],losers=Array.isArray(features.top_losers)?features.top_losers.slice(0,5):[];
    const maximum=Math.max(1,...[...gainers,...losers].map(row=>finite(row.amount_change)?Math.abs(row.amount_change):0));
    node('[data-market-movers]').innerHTML=`<div class="market-movers-heading"><h3>成交额增减</h3><span>各 Top5 · ${amountUnit(features.units)}</span></div>${[['增加',gainers],['减少',losers]].map(([label,rows])=>`<div class="market-movers-group"><h4>${label}</h4>${rows.length?rows.map(row=>`<button class="market-mover" type="button" data-market-select="${escapeHtml(row.board_id)}" aria-label="${escapeHtml(`${row.name || row.board_id}，成交额变化 ${marketAmount(row.amount_change,features.units,true)} ${amountUnit(features.units)}`)}"><span class="market-mover-name">${escapeHtml(row.name || row.board_id)}</span><span class="market-mover-track"><i class="${tone(row.amount_change)}" style="width:${finite(row.amount_change)?Math.abs(row.amount_change)/maximum*100:0}%"></i></span><strong class="${tone(row.amount_change)}">${marketAmount(row.amount_change,features.units,true)}</strong></button>`).join(''):'<p class="small muted">暂无同口径可比数据</p>'}</div>`).join('')}`;
  }
  function renderTable(){
    const rows=features.boards || [],units=features.units || {};
    node('[data-market-row-count]').textContent=`${kindNames[features.kind || node('[data-market-kind]').value]}${finite(features.filtered_boards)?`匹配 ${features.filtered_boards} 个板块 · `:' · '}本次返回 ${rows.length} 行`;
    node('[data-market-table]').innerHTML=table([{label:'板块',render:row=>`<button type="button" class="row-link" data-market-select="${escapeHtml(row.board_id)}" aria-pressed="${row.board_id===selected?.board_id}">${escapeHtml(row.name || row.board_id)}</button><div class="dataset-name">${escapeHtml(row.board_id)} · ${escapeHtml(kindNames[row.kind] || row.kind || '未细分')}</div>`},{label:`成交额（${amountUnit(units)}）`,render:row=>marketAmount(row.amount,units)},{label:`成交额增减（${amountUnit(units)}）`,render:row=>`<span class="${tone(row.amount_change)}">${marketAmount(row.amount_change,units,true)}</span>`},{label:'增减幅',render:row=>`<span class="${tone(row.amount_change_pct)}">${signedPercent(row.amount_change_pct)}</span>`},{label:'当日涨跌',render:row=>`<span class="${tone(row.return_1)}">${signedPercent(row.return_1)}</span>`},{label:'相对额20日',render:row=>number(row.relative_amount20)},{label:'市场占比',render:row=>percent(row.market_share)},{label:'成员核验',render:row=>`${number(row.coverage?.available,0)} / ${number(row.coverage?.expected,0)}<div class="small muted">可比 ${number(row.coverage?.comparable,0)}</div>`}],rows,{emptyTitle:'该类型尚无可用板块',emptyDescription:'调整类型或查看来源警告；没有结果不会切换到另一份口径。'});
  }
  function renderMap(){
    mapChart?.destroy();
    const metric=node('[data-market-color]').value;
    node('[data-market-color-legend]').textContent=metric==='return_1'?'颜色＝当日涨跌幅':'颜色＝成交额较前日增减';
    mapChart=createMarketTreemap(node('[data-market-map]'),features.boards || [],{units:features.units,colorMetric:metric,selected:selected?.board_id,onSelect:chooseBoard});
    const excluded=(features.boards || []).filter(row=>!finite(row.amount)||row.amount<=0).length;
    node('[data-market-map-caption]').textContent=`${basisName(features.basis)} · ${amountUnit(features.units)}${excluded?`；${excluded} 个零值或缺额板块无面积`:''}。可通过下方表格键盘选择板块。`;
  }
  function renderTrend(){
    trendChart?.destroy();trendChart=null;
    if(!history)return;
    try{trendChart=createMarketTrend(node('[data-market-trend]'),history.points || [],{units:history.units || features.units,metric:node('[data-market-trend-metric]').value});}
    catch(error){node('[data-market-trend]').innerHTML=errorHtml(error);}
    node('[data-market-history-notes]').innerHTML=boardFeaturePanel(selected,features.units,features.basis)+disclosure('走势口径与来源说明',{warnings:history.warnings,view_id:view.view_id,read_ref:history.read_ref,membership_date:history.membership_date,training_eligible:false});
    const points=history.points || [];
    node('[data-market-history-caption]').textContent=`请求 ${windowSize} 交易日，实际返回 ${points.length} 点${points.length?`，${points[0].date} 至 ${points.at(-1).date}`:''}；缺值保持缺口，不插值。`;
  }
  async function loadMembers(){
    if(!membersOpen || !view?.view_id || !selected)return;
    membersController?.abort();membersController=new AbortController();
    const current=++membersSequence,identity=view.view_id,boardId=selected.board_id;
    node('[data-market-members-content]').innerHTML='<div class="loading-state">正在读取冻结成员…</div>';
    node('[data-market-members-prev]').disabled=true;node('[data-market-members-next]').disabled=true;
    try{
      const result=await api.marketMembers(boardId,{view_id:identity,offset:membersOffset,limit:100},requestSignal(membersController));
      if(signal.aborted || current!==membersSequence || identity!==view?.view_id || boardId!==selected?.board_id)return;
      if(result.view_id&&result.view_id!==identity || result.board_id&&result.board_id!==boardId || result.read_ref&&features.read_ref&&result.read_ref!==features.read_ref)throw new Error('成员读取身份与冻结板块不一致，未展示其他版本。');
      membersTotal=result.total_count ?? 0;
      const rows=Array.isArray(result.rows)?result.rows:[];
      const units=result.units || features.units || {};
      node('[data-market-members-count]').textContent=`${number(membersTotal,0)} 个匹配成员`;
      node('[data-market-members-content]').innerHTML=`${marketWarnings(result.warnings)}${result.status&&result.status!=='available'&&result.status!=='ready'?`<div class="market-unit-note">成员状态：${escapeHtml(result.status)}；只展示已有核验映射，不按名称匹配板块。</div>`:''}${table([{label:'股票',render:row=>`<a href="${escapeHtml(routeLink('analysis',{symbol:row.symbol,end:features.price_date}))}">${escapeHtml(row.symbol)}</a>${row.name?`<div class="small muted">${escapeHtml(row.name)}</div>`:''}`},{label:'收盘',render:row=>number(row.close)},{label:'当日涨跌',render:row=>`<span class="${tone(row.return_1)}">${signedPercent(row.return_1)}</span>`},{label:`成交额（${amountUnit(units)}）`,render:row=>marketAmount(row.amount,units)}],rows,{emptyTitle:'没有可读的核验成员',emptyDescription:'该来源可能尚无可靠成员映射或对应日期未发布；不会按板块名称猜测成员。'})}`;
      node('[data-market-members-position]').textContent=rows.length?`${membersOffset+1}–${membersOffset+rows.length} / ${membersTotal}`:`0 / ${membersTotal}`;
      node('[data-market-members-prev]').disabled=membersOffset===0;
      node('[data-market-members-next]').disabled=membersOffset+100>=membersTotal;
    }catch(error){if(error.name!=='AbortError')node('[data-market-members-content]').innerHTML=errorHtml(error);}
  }
  async function chooseBoard(row){
    if(!row || !view?.view_id)return;
    selected=row;membersOffset=0;history=null;
    detailController?.abort();membersController?.abort();detailController=new AbortController();
    const current=++detailSequence,identity=view.view_id;
    mapChart?.select(row.board_id);
    host.querySelectorAll('[data-market-select]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.marketSelect===row.board_id)));
    node('[data-market-board-title]').textContent=`${row.name || row.board_id} · 近期成交变化`;
    node('[data-market-board-meta]').textContent=`${row.board_id} · ${basisName(features.basis)} · 行情 ${features.price_date || '—'} · 成员 ${features.membership_date || '—'}`;
    node('[data-market-members-toggle]').disabled=false;
    trendChart?.destroy();trendChart=null;
    node('[data-market-trend]').innerHTML='<div class="loading-state">正在读取冻结版本走势…</div>';
    node('[data-market-history-notes]').innerHTML=boardFeaturePanel(row,features.units,features.basis);node('[data-market-history-caption]').textContent='';
    if(membersOpen)loadMembers();
    try{
      const result=await api.marketHistory(row.board_id,{view_id:identity,window:windowSize},requestSignal(detailController));
      if(signal.aborted || current!==detailSequence || identity!==view?.view_id)return;
      if(result.view_id&&result.view_id!==identity || result.board_id&&result.board_id!==row.board_id || result.read_ref&&features.read_ref&&result.read_ref!==features.read_ref)throw new Error('走势读取身份与冻结板块不一致，未拼接其他版本。');
      history=result;renderTrend();
    }catch(error){if(error.name!=='AbortError')node('[data-market-trend]').innerHTML=errorHtml(error);}
  }
  async function loadSpecialty(){
    if(tab==='boards')return;
    specialController?.abort();specialController=new AbortController();
    const current=++specialSequence,target=node('[data-market-specialty-content]');
    if(!view?.view_id){target.innerHTML=empty('先生成一份行情视图','各专项读取共用冻结身份，不会在切换页签时补采。');return;}
    const identity=view.view_id,section=node('[data-market-section]').value || 'breadth',key=`${identity}:${section}`;
    target.innerHTML='<div class="loading-state">正在读取已发布专项数据…</div>';
    try{
      const response=specialtyCache.get(key) || await api.marketSpecialties({view_id:identity,section},requestSignal(specialController));
      if(signal.aborted || current!==specialSequence || identity!==view?.view_id)return;
      if(response.read_ref&&view.read_ref&&response.read_ref!==view.read_ref)throw new Error('专项读取凭证与当前冻结视图不一致，未展示其他版本。');
      specialtyCache.set(key,response);
      target.innerHTML=(response.panels || []).map(specialtyPanel).join('') || empty('该主题没有可读面板','查看下方能力与来源凭证；不自动改用其他来源。');
      node('[data-market-specialty-receipt]').innerHTML=disclosure('专项冻结读取凭证',{view_id:identity,section,read_ref:response.read_ref,warnings:response.warnings});
    }catch(error){if(error.name!=='AbortError')target.innerHTML=errorHtml(error);}
  }
  function setTab(value){
    tab=value;
    host.querySelectorAll('[data-market-tab]').forEach(button=>{const active=button.dataset.marketTab===value;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;});
    node('[data-market-boards]').hidden=value!=='boards';node('[data-market-specialty]').hidden=value==='boards';
    const choices=value==='breadth'?['breadth']:value==='capital'?['valuation','flows']:['industry','derivatives','macro','intraday'];
    node('[data-market-section]').innerHTML=choices.map(section=>`<option value="${section}">${sectionNames[section]}</option>`).join('');
    node('[data-market-section-label]').hidden=choices.length===1;
    if(value!=='boards'){
      node('[data-market-specialty]').setAttribute('aria-labelledby',`market-tab-${value}`);
      loadSpecialty();
    }
  }
  async function loadFeatures(){
    if(!view?.view_id)return;
    featuresController?.abort();detailController?.abort();membersController?.abort();featuresController=new AbortController();
    const current=++featuresSequence,identity=view.view_id;
    node('[data-market-error]').innerHTML='';
    node('[data-market-view-status]').textContent='正在读取冻结视图…';
    try{
      const result=await api.marketFeatures({view_id:identity,kind:node('[data-market-kind]').value,sort_by:node('[data-market-sort]').value,limit:30},requestSignal(featuresController));
      if(signal.aborted || current!==featuresSequence || identity!==view?.view_id)return;
      if(result.view_id!==identity)throw new Error('行情读取返回了另一份视图，未替换当前结果。');
      if(result.read_ref&&view.read_ref&&result.read_ref!==view.read_ref)throw new Error('行情来源凭证与冻结视图不一致，未替换当前结果。');
      features=result;
      node('[data-market-view-status]').textContent=`${basisName(result.basis)} · ${result.price_date || '日期待核验'} · 冻结视图 ${identity.slice(0,12)}`;
      node('[data-market-basis-label]').textContent=`${basisName(result.basis)} · ${amountUnit(result.units)} · 按已发布来源分类，不按名称拼接`;
      const warnings=notes(result.warnings);
      node('[data-market-warnings]').innerHTML=marketWarnings(warnings.slice(0,3))+(warnings.length>3?disclosure(`其他来源说明（${warnings.length-3}条）`,warnings.slice(3)):'');
      node('[data-market-receipt]').innerHTML=disclosure('数据口径、冻结版本与读取凭证',{view_id:identity,read_ref:result.read_ref,basis:result.basis,price_date:result.price_date,membership_date:result.membership_date,units:result.units,summary:result.summary,warnings:result.warnings,training_eligible:result.training_eligible,capabilities});
      renderMetrics();renderMovers();renderTable();renderMap();
      const next=(result.boards || []).find(row=>row.board_id===selected?.board_id) || result.boards?.[0];
      if(next)await chooseBoard(next);
      else{selected=null;history=null;trendChart?.destroy();trendChart=null;node('[data-market-trend]').innerHTML=empty('该筛选没有可选板块','切换板块类型；不借用上一类型的走势。');node('[data-market-board-title]').textContent='选择一个板块';node('[data-market-board-meta]').textContent='当前筛选没有板块';node('[data-market-members-toggle]').disabled=true;node('[data-market-members-content]').innerHTML='';node('[data-market-history-caption]').textContent='';node('[data-market-history-notes]').innerHTML='';}
    }catch(error){if(error.name!=='AbortError'){showPageError(error);node('[data-market-view-status]').textContent='冻结视图读取失败；未改用其他数据';}}
  }
  host.querySelectorAll('[data-market-tab]').forEach(button=>{
    button.setAttribute('aria-controls',button.dataset.marketTab==='boards'?'market-panel-boards':'market-panel-specialty');
    button.addEventListener('click',()=>setTab(button.dataset.marketTab),{signal});
    button.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const buttons=[...host.querySelectorAll('[data-market-tab]')],index=buttons.indexOf(button),next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;buttons[next].focus();setTab(buttons[next].dataset.marketTab);},{signal});
  });
  node('[data-market-section]').addEventListener('change',loadSpecialty,{signal});
  for(const selector of ['[name=basis]','[name=end]'])node(selector).addEventListener('change',()=>{node('[data-market-scope-note]').textContent='筛选尚未生成新视图；下方仍是原冻结版本。点击“生成新视图”应用新的数据口径或日期。';},{signal});
  for(const selector of ['[data-market-kind]','[data-market-sort]'])node(selector).addEventListener('change',loadFeatures,{signal});
  node('[data-market-color]').addEventListener('change',()=>{if(features)renderMap();},{signal});
  node('[data-market-trend-metric]').addEventListener('change',renderTrend,{signal});
  host.querySelectorAll('[data-market-window]').forEach(button=>button.addEventListener('click',()=>{windowSize=Number(button.dataset.marketWindow);host.querySelectorAll('[data-market-window]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));if(selected)chooseBoard(selected);},{signal}));
  host.addEventListener('click',event=>{const button=event.target.closest('[data-market-select]');if(!button || !features)return;const row=[...(features.boards || []),...(features.top_gainers || []),...(features.top_losers || [])].find(item=>item.board_id===button.dataset.marketSelect);if(row)chooseBoard(row);},{signal});
  node('[data-market-members-toggle]').addEventListener('click',()=>{membersOpen=!membersOpen;node('[data-market-members]').hidden=!membersOpen;node('[data-market-members-toggle]').textContent=membersOpen?'收起成员':'查看成员';node('[data-market-members-toggle]').setAttribute('aria-expanded',String(membersOpen));if(membersOpen)loadMembers();else membersController?.abort();},{signal});
  node('[data-market-members-prev]').addEventListener('click',()=>{membersOffset=Math.max(0,membersOffset-100);loadMembers();},{signal});
  node('[data-market-members-next]').addEventListener('click',()=>{if(membersOffset+100<membersTotal){membersOffset+=100;loadMembers();}},{signal});
  node('[data-market-prepare-form]').addEventListener('submit',async event=>{
    event.preventDefault();
    try{
      const form=event.currentTarget,data=new FormData(form),body={basis:data.get('basis')};
      if(data.get('end'))body.end=data.get('end');
      const result=await busy(node('[data-market-prepare]'),()=>api.prepareMarket(body,signal),'正在冻结已发布数据…');
      if(signal.aborted)return;
      const next=result.view || result;
      if(!next.view_id)throw new Error('服务没有返回冻结视图身份，未切换数据。');
      cancelReads();mapChart?.destroy();trendChart?.destroy();mapChart=null;trendChart=null;features=null;selected=null;history=null;
      view=next;specialtyCache.clear();noView();node('[data-market-prepare]').textContent='生成新视图';node('[data-market-scope-note]').textContent='已显式生成新的本地冻结视图；不启动任何来源补采。';
      await loadFeatures();if(tab!=='boards')await loadSpecialty();
    }catch(error){showPageError(error);}
  },{signal});
  noView();setTab(tab);
  try{
    const status=await api.marketStatus(signal);
    if(!signal.aborted){capabilities=status.capabilities || {};view=status.latest_view;
      node('[data-market-prepare]').disabled=false;
      if(view?.view_id){node('[name=basis]').value=view.basis || 'members';if(view.price_date)node('[name=end]').value=view.price_date;node('[data-market-prepare]').textContent='生成新视图';await loadFeatures();if(tab!=='boards')await loadSpecialty();}
      else noView();
    }
  }catch(error){if(error.name!=='AbortError'){if(error.status===404)showPageError(new Error('后端尚未载入，请等当前任务结束后切换服务，再刷新本页。'));else{showPageError(error);node('[data-market-prepare]').disabled=false;}node('[data-market-view-status]').textContent='行情服务未就绪，不代表数据湖没有数据';}}
  return()=>{cancelReads();signal.removeEventListener('abort',cancelReads);mapChart?.destroy();trendChart?.destroy();};
}
