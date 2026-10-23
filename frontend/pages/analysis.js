import {api} from '../core/api.js';
import {arrayOf,badge,bindSymbolSearch,busy,dataTable,day,disclosure,empty,errorHtml,escapeHtml,formValue,heading,notify,number,routeLink,showError,symbolsFrom,today,validateDates,yearStart} from '../core/ui.js';
import {createResearchChart} from '../core/charts.js';

export function evidenceRows(result) {
  const inputs=[...(result?.source_reference_points || []).map(row=>({...row,family:row.source || 'structure'})),...(result?.wyckoff_events || []).map(row=>({...row,family:'wyckoff'})),...(result?.ict_events || []).map(row=>({...row,family:'ict'}))];
  const unique=new Map();
  inputs.forEach((row,index)=>{
    const identity=row.event_id?JSON.stringify([row.symbol || result?.symbol,row.family,row.event_id]):`anonymous:${index}`;
    const defined=Object.fromEntries(Object.entries(row).filter(([,value])=>value!==undefined && value!==null));
    unique.set(identity,{...unique.get(identity),...defined});
  });
  return [...unique.values()].sort((left,right)=>String(right.first_available_at || right.available_at || right.time || right.date).localeCompare(String(left.first_available_at || left.available_at || left.time || left.date)));
}

export async function mount(host,{signal,params}) {
  let result;
  let frequency='日线';
  let chart;
  let selectedDate='';
  let featureCleanup;
  host.innerHTML=heading('结构与量价','围绕同一份行情，查看已确认结构、威科夫与 ICT 来源证据。',`<button type="button" data-add-watch disabled>加入关注</button><a class="button" data-open-backtest href="#backtest">配置回测</a>`)+`<section class="panel"><form id="analysis-form" class="form-toolbar"><div class="form-grid"><label>股票代码<input id="analysis-symbol" name="symbol" value="${escapeHtml(params.get('symbol') || '')}" placeholder="例如 600519.SH" required autocomplete="off"></label><label>显示开始<input name="start" type="date" value="${escapeHtml(params.get('start') || yearStart())}"></label><label>行情与可知截止<input name="end" type="date" value="${escapeHtml(params.get('end') || today())}" required></label><button class="primary" type="submit">分析结构</button></div><p class="form-note">默认结构与量价；图表周期与图层切换仅改变展示。输入先按截止日截断。</p></form><div class="chart-toolbar"><div class="segmented" aria-label="K线周期">${['日线','周线','月线'].map(value=>`<button type="button" data-frequency="${value}" aria-pressed="${value===frequency}">${value}</button>`).join('')}</div><div class="chart-layers">${[['pens','笔'],['zones','中枢'],['fractals','分型'],['sources','来源 B/S'],['wyckoff','威科夫'],['ict','ICT'],['volume','成交量']].map(([key,label])=>`<label><input type="checkbox" data-layer="${key}" ${key==='fractals'?'':'checked'}>${label}</label>`).join('')}</div><button class="small-button" type="button" data-fit>适应范围</button></div><div class="chart-readout" data-chart-readout><strong>等待分析</strong><span class="muted">悬停查看真实 OHLC</span></div><div class="chart-area" data-analysis-chart>${empty('从一只股票开始','填写股票与截止日期，读取结构及量价证据。新分析需要实际数据；这里不会显示样例行情。')}</div><div class="chart-caption" data-chart-caption>来源参考点是研究证据，实际成交在模拟账本中核验。</div><div data-analysis-warnings></div></section><div class="grid grid-even space-top"><section class="panel"><div class="panel-header"><h2>事件与依据</h2><div class="button-row"><select data-event-family aria-label="事件来源"><option value="">全部来源</option><option value="structure">结构</option><option value="wyckoff">威科夫</option><option value="ict">ICT</option></select><button class="small-button" data-clear-date hidden>清除日期</button></div></div><div class="evidence-list" data-evidence>${empty('尚无本次事件','分析后显示原始确认时间、来源及方向；点击图表可按日期查看。')}</div></section><section class="panel"><div class="panel-header"><h2>方法特征与数据身份</h2><button class="small-button" data-features disabled>读取特征</button></div><div class="panel-body" data-analysis-identity>${empty('等待分析身份','数据版本、价格口径与输入质量随每次计算返回。')}</div><div data-feature-output></div></section></div>`;
  const form=host.querySelector('#analysis-form');
  bindSymbolSearch(host.querySelector('#analysis-symbol'),signal);
  function events(){
    const family=host.querySelector('[data-event-family]').value;
    const period=value=>globalThis.KHQuantMarkers?.periodKey(value,frequency) || day(value);
    const rows=evidenceRows(result).filter(row=>(!family||row.family===family)&&(!selectedDate||period(row.first_available_at || row.available_at || row.time || row.date)===period(selectedDate)));
    host.querySelector('[data-clear-date]').hidden=!selectedDate;
    host.querySelector('[data-clear-date]').textContent=selectedDate?`清除 ${selectedDate}`:'清除日期';
    host.querySelector('[data-evidence]').innerHTML=rows.length?rows.slice(0,100).map(row=>`<article class="evidence-row"><div class="evidence-top"><span>${escapeHtml(({structure:'CZSC 结构',wyckoff:'威科夫量价',ict:'ICT 流动性',ma:'均线'})[row.family] || row.family)}</span><time>${escapeHtml(day(row.first_available_at || row.available_at || row.time || row.date))}</time></div><strong>${escapeHtml(row.label || row.name || row.event || row.event_type || row.type || row.action || row.side || '来源事件')}</strong><div class="button-row">${badge(row.confirmed===true || row.is_confirmed===true?'verified':'reference',row.confirmed===true || row.is_confirmed===true?'已确认':'按原始状态保存')}<span class="small ${['BUY','buy','bullish'].includes(row.action || row.side || row.direction)?'up':['SELL','sell','bearish'].includes(row.action || row.side || row.direction)?'down':'muted'}">${escapeHtml(row.action || row.side || row.direction || '')}</span></div><p>可用：${escapeHtml(row.first_available_at || row.available_at || '未返回')}</p>${row.confirmed_at || row.first_confirmed_at?`<p>确认：${escapeHtml(row.first_confirmed_at || row.confirmed_at)}</p>`:''}<p>${escapeHtml(row.reason || row.description || row.reference_display_label || '')}</p>${row.reference_price!==undefined?`<p>来源参考价 ${number(row.reference_price)} · 非成交价</p>`:''}</article>`).join(''):empty('没有匹配的来源事件',selectedDate?'清除日期或切换来源筛选。':'当前输入与条件下没有返回来源点；不代表历史从未发生。');
  }
  function draw(fit=true){
    if(!result)return;
    try{
      if(!chart)chart=createResearchChart(host.querySelector('[data-analysis-chart]'),{readout:host.querySelector('[data-chart-readout]'),onSelect:date=>{selectedDate=date;events();}});
      const layers=Object.fromEntries([...host.querySelectorAll('[data-layer]')].map(input=>[input.dataset.layer,input.checked]));
      const summary=chart.render(result,{frequency,layers,fit});
      host.querySelector('[data-chart-caption]').textContent=`${result.symbol} · ${frequency} ${summary.bars} 根${summary.forming?`，${summary.forming} 根形成中`:''} · 截至 ${result.as_of || result.data_end || '—'} · 来源参考点独立于实际成交`;
    }catch(error){showError(host,error);}
  }
  host.querySelectorAll('[data-frequency]').forEach(button=>button.addEventListener('click',()=>{frequency=button.dataset.frequency;selectedDate='';host.querySelectorAll('[data-frequency]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));draw();events();},{signal}));
  host.querySelectorAll('[data-layer]').forEach(input=>input.addEventListener('change',()=>draw(false),{signal}));
  host.querySelector('[data-fit]').addEventListener('click',()=>chart?.fit(),{signal});
  host.querySelector('[data-event-family]').addEventListener('change',events,{signal});
  host.querySelector('[data-clear-date]').addEventListener('click',()=>{selectedDate='';events();},{signal});
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const errors=host.querySelector('[data-page-error]');errors.replaceChildren();
    try{
      const symbol=symbolsFrom(formValue(form,'symbol'),1)[0],start=formValue(form,'start'),end=formValue(form,'end');validateDates(start,end);
      const response=await busy(form.querySelector('[type=submit]'),()=>api.analyze({symbol,start:start || null,end,analysis_method:'structure'},signal),'正在分析…');
      if(signal.aborted)return;
      result=response.result || response;
      selectedDate='';
      draw();events();
      host.querySelector('[data-add-watch]').disabled=false;
      host.querySelector('[data-features]').disabled=false;
      host.querySelector('[data-open-backtest]').href=routeLink('backtest',{symbols:result.symbol || symbol,start,end});
      host.querySelector('[data-analysis-identity]').innerHTML=`<dl class="detail-list"><div><dt>数据版本</dt><dd class="inline-code">${escapeHtml(typeof result.data_version==='object'?JSON.stringify(result.data_version):result.data_version || '未返回')}</dd></div><div><dt>价格口径</dt><dd>${escapeHtml(typeof result.price_basis==='object'?JSON.stringify(result.price_basis):result.price_basis || '未返回')}</dd></div><div><dt>方法</dt><dd>${escapeHtml(result.analysis_method || 'structure')}</dd></div><div><dt>实际输入</dt><dd>${escapeHtml(result.data_range?.start || '—')} 至 ${escapeHtml(result.data_range?.end || result.data_end || '—')} / ${number(result.data_range?.bars,0)} 根</dd></div><div><dt>引擎</dt><dd>${escapeHtml(result.czsc_version || result.strategy_version || '未返回')}</dd></div></dl>`;
      host.querySelector('[data-analysis-warnings]').innerHTML=disclosure('输入质量与研究边界',{input_quality:result.input_quality,warmup:result.warmup,warnings:result.warnings});
      host.querySelector('[data-feature-output]').replaceChildren();
      notify('已读取本次结构与量价分析');
    }catch(error){showError(host,error);}
  },{signal});
  host.querySelector('[data-add-watch]').addEventListener('click',async event=>{if(!result)return;try{await busy(event.currentTarget,()=>api.addWatch(result.symbol,signal),'保存中…');notify(`已关注 ${result.symbol}`);}catch(error){showError(host,error);}},{signal});
  host.querySelector('[data-features]').addEventListener('click',async event=>{
    if(!result)return;
    try{
      const response=await busy(event.currentTarget,()=>api.features({symbol:result.symbol,end:result.data_end || formValue(form,'end')},signal),'读取中…');if(signal.aborted)return;
      const rows=arrayOf(response,'rows'),columns=response.columns || [],latest=rows.at(-1) || {};
      const numeric=Array.isArray(response.feature_columns)?response.feature_columns:columns.filter(column=>!['date','time','symbol','stock'].includes(column) && typeof latest[column]==='number');
      const reasons=Array.isArray(latest.score_reason_codes)?latest.score_reason_codes.join('；'):latest.score_reason_codes;
      host.querySelector('[data-feature-output]').innerHTML=`<div class="feature-group"><h3>${escapeHtml(response.profile || '方法特征')} · 最近可读时点</h3><div class="button-row">${badge(latest.score_input_eligible===true?'verified':latest.score_input_eligible===false?'unavailable':'unknown',latest.score_input_eligible===true?'特征输入合格':latest.score_input_eligible===false?'特征输入未通过':'特征资格未返回')}<span class="small muted">${escapeHtml(reasons || '')}</span></div><div class="feature-values space-top">${numeric.map(column=>`<div><span>${escapeHtml(column)}</span><strong>${number(latest[column],5)}</strong></div>`).join('') || '<p class="small muted">未返回可展示数值。质量与完整列序保持原格式可读。</p>'}</div></div><div class="schema-list">${columns.map(column=>`<span>${escapeHtml(column)}</span>`).join('')}</div>${disclosure('特征列序、来源与质量',{feature_columns:response.feature_columns,columns,source:response.source,profile:response.profile,quality:response.quality,score_input_eligible:latest.score_input_eligible,score_reason_codes:latest.score_reason_codes})}${disclosure('最近特征行',rows.slice(-20))}`;
    }catch(error){showError(host,error);}
  },{signal});
  if(params.get('report')){
    try{const response=await api.report(params.get('report'),signal);if(!signal.aborted){result=response.result || response;draw();events();}}catch(error){showError(host,error);}
  }
  return()=>{chart?.destroy();featureCleanup?.();};
}
