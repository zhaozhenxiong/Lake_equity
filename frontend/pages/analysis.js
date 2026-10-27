import {api} from '../core/api.js';
import {arrayOf,badge,bindSymbolSearch,busy,dataTable,day,disclosure,empty,errorHtml,escapeHtml,formValue,heading,notify,number,percent,routeLink,showError,symbolsFrom,today,validateDates,yearStart} from '../core/ui.js';
import {createResearchChart} from '../core/charts.js';
import {submitJob,watchJob} from '../core/jobs.js';
import {analysisPredictionCompatible,predictionQuality,predictionReference} from '../core/prediction.js';

export function analysisInputsMatch(result,form){
  try{return symbolsFrom(formValue(form,'symbol'),1)[0]===result?.symbol && formValue(form,'end')===day(result.as_of || result.data_end);}
  catch{return false;}
}

export function analysisPredictionSpec(result,horizon){
  if(!result?.input_manifest_id || !(result.lake_run_id || result.run_id) || !result.symbol)throw new Error('请先生成具有完整输入身份的结构分析。');
  if(![1,5].includes(Number(horizon)))throw new Error('分析页预测周期仅支持1或5个交易日。');
  const cutoff=day(result.as_of || result.data_end);
  if(result.data_end && day(result.data_end)!==cutoff)throw new Error('实际行情截止与分析身份不一致，请先重新分析。');
  return {analysis_run_id:result.lake_run_id || result.run_id,symbol:result.symbol,end:cutoff,lookback:128,horizon:Number(horizon),samples:8,seed:42,device:'auto'};
}

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

export function applyAnalysisResult(host,result){
  const form=host.querySelector('#analysis-form');
  const symbol=result.symbol || formValue(form,'symbol');
  const start=result.start || result.data_range?.start || formValue(form,'start');
  const end=result.end || result.as_of || result.data_end || result.data_range?.end || formValue(form,'end');
  form.querySelector('[name=symbol]').value=symbol;
  form.querySelector('[name=start]').value=day(start);
  form.querySelector('[name=end]').value=day(end);
  host.querySelector('[data-add-watch]').disabled=!symbol;
  host.querySelector('[data-features]').disabled=!symbol;
  host.querySelector('[data-open-backtest]').href=routeLink('backtest',{symbols:symbol,start:day(start),end:day(end)});
  host.querySelector('[data-analysis-identity]').innerHTML=`<dl class="detail-list"><div><dt>数据版本</dt><dd class="inline-code">${escapeHtml(typeof result.data_version==='object'?JSON.stringify(result.data_version):result.data_version || '未返回')}</dd></div><div><dt>价格口径</dt><dd>${escapeHtml(typeof result.price_basis==='object'?JSON.stringify(result.price_basis):result.price_basis || '未返回')}</dd></div><div><dt>方法</dt><dd>${escapeHtml(result.analysis_method || 'structure')}</dd></div><div><dt>实际输入</dt><dd>${escapeHtml(result.data_range?.start || '—')} 至 ${escapeHtml(result.data_range?.end || result.data_end || '—')} / ${number(result.data_range?.bars,0)} 根</dd></div><div><dt>引擎</dt><dd>${escapeHtml(result.czsc_version || result.strategy_version || '未返回')}</dd></div></dl>`;
  host.querySelector('[data-analysis-warnings]').innerHTML=disclosure('输入质量与研究边界',{input_quality:result.input_quality,warmup:result.warmup,warnings:result.warnings});
  host.querySelector('[data-feature-output]').replaceChildren();
}

export async function mount(host,{signal,params}) {
  let result;
  let frequency='日线';
  let chart;
  let selectedDate='';
  let featureCleanup;
  let predictionWatcher,predictionRecord=null,analysisVersion=0,predictionSequence=0,analysisSequence=0,appliedInputs='';
  host.innerHTML=heading('结构与量价','围绕同一份行情，查看已确认结构、威科夫与 ICT 来源证据。',`<button type="button" data-add-watch disabled>加入关注</button><a class="button" data-open-backtest href="#backtest">配置回测</a>`)+`<section class="panel"><form id="analysis-form" class="form-toolbar"><div class="form-grid"><label>股票代码<input id="analysis-symbol" name="symbol" value="${escapeHtml(params.get('symbol') || '')}" placeholder="例如 600519.SH" required autocomplete="off"></label><label>显示开始<input name="start" type="date" value="${escapeHtml(params.get('start') || yearStart())}"></label><label>行情与可知截止<input name="end" type="date" value="${escapeHtml(params.get('end') || today())}" required></label><button class="primary" type="submit">分析结构</button></div><p class="form-note">默认结构与量价；图表周期与图层切换仅改变展示。输入先按截止日截断。</p></form><div class="chart-toolbar"><div class="segmented" aria-label="K线周期">${['日线','周线','月线'].map(value=>`<button type="button" data-frequency="${value}" aria-pressed="${value===frequency}">${value}</button>`).join('')}</div><div class="chart-layers">${[['pens','笔'],['zones','中枢'],['fractals','分型'],['sources','来源 B/S'],['wyckoff','威科夫'],['ict','ICT'],['volume','成交量']].map(([key,label])=>`<label><input type="checkbox" data-layer="${key}" ${key==='fractals'?'':'checked'}>${label}</label>`).join('')}</div><button class="small-button" type="button" data-fit>适应范围</button></div><div class="chart-readout" data-chart-readout><strong>等待分析</strong><span class="muted">悬停查看真实 OHLC</span></div><div class="chart-area" data-analysis-chart>${empty('从一只股票开始','填写股票与截止日期，读取结构及量价证据。新分析需要实际数据；这里不会显示样例行情。')}</div><div class="chart-caption" data-chart-caption>来源参考点是研究证据，实际成交在模拟账本中核验。</div><div data-analysis-warnings></div></section><div class="grid grid-even space-top"><section class="panel"><div class="panel-header"><h2>事件与依据</h2><div class="button-row"><select data-event-family aria-label="事件来源"><option value="">全部来源</option><option value="structure">结构</option><option value="wyckoff">威科夫</option><option value="ict">ICT</option></select><button class="small-button" data-clear-date hidden>清除日期</button></div></div><div class="evidence-list" data-evidence>${empty('尚无本次事件','分析后显示原始确认时间、来源及方向；点击图表可按日期查看。')}</div></section><section class="panel"><div class="panel-header"><h2>方法特征与数据身份</h2><button class="small-button" data-features disabled>读取特征</button></div><div class="panel-body" data-analysis-identity>${empty('等待分析身份','数据版本、价格口径与输入质量随每次计算返回。')}</div><div data-feature-output></div></section></div>`;
  const form=host.querySelector('#analysis-form');
  const predictionPanel=document.createElement('section');
  predictionPanel.className='analysis-prediction-controls';
  predictionPanel.innerHTML='<div class="button-row"><strong>Kronos预测</strong><span class="small muted" data-prediction-context>先读取结构分析</span><label class="field-inline">未来交易日<select data-prediction-horizon aria-label="预测交易日"><option value="1">1</option><option value="5" selected>5</option></select></label><button type="button" class="small-button primary" data-run-analysis-prediction disabled>运行预测</button><label class="check-field"><input type="checkbox" data-prediction-layer checked>显示预测层</label></div><p class="form-note">P50虚线与P10–P90采样分位带仅为研究情景；上涨采样比例不是校准概率，分位带不是置信区间。预测变化相对截止日收盘，非可交易收益；1日预测不代表T+1可实现收益。</p><div data-analysis-prediction-state></div><div class="job-inline" data-analysis-prediction-job hidden></div>';
  host.querySelector('[data-analysis-chart]').before(predictionPanel);
  const predictionButton=predictionPanel.querySelector('[data-run-analysis-prediction]');
  const predictionState=predictionPanel.querySelector('[data-analysis-prediction-state]');
  const inputKey=()=>['symbol','start','end'].map(name=>formValue(form,name)).join('|');
  const inputsCurrent=()=>!!result && appliedInputs===inputKey() && analysisInputsMatch(result,form);
  function updatePredictionControls(){
    predictionButton.disabled=!inputsCurrent() || !result?.input_manifest_id;
    predictionPanel.querySelector('[data-prediction-context]').textContent=!result?'先读取结构分析':inputsCurrent()?`${result.symbol} · 实际截止 ${day(result.as_of || result.data_end)}${frequency==='日线'?'':' · 预测按日线生成，切回日线查看'}`:'输入已修改，请先运行结构分析；旧预测未叠加';
  }
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
      const prediction=inputsCurrent() && predictionPanel.querySelector('[data-prediction-layer]').checked?predictionRecord:null;
      const summary=chart.render(result,{frequency,layers,fit,prediction});
      host.querySelector('[data-chart-caption]').textContent=`${result.symbol} · ${frequency} ${summary.bars} 根${summary.forming?`，${summary.forming} 根形成中`:''} · 截至 ${result.as_of || result.data_end || '—'} · 来源参考点独立于实际成交`;
    }catch(error){showError(host,error);}
  }
  host.querySelectorAll('[data-frequency]').forEach(button=>button.addEventListener('click',()=>{frequency=button.dataset.frequency;selectedDate='';host.querySelectorAll('[data-frequency]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));draw();events();updatePredictionControls();},{signal}));
  host.querySelectorAll('[data-layer]').forEach(input=>input.addEventListener('change',()=>draw(false),{signal}));
  host.querySelector('[data-fit]').addEventListener('click',()=>chart?.fit(),{signal});
  host.querySelector('[data-event-family]').addEventListener('change',events,{signal});
  host.querySelector('[data-clear-date]').addEventListener('click',()=>{selectedDate='';events();},{signal});
  function showResult(response){
    ++analysisVersion;++predictionSequence;predictionWatcher?.stop();predictionRecord=null;predictionState.replaceChildren();
    const previousJob=predictionPanel.querySelector('[data-analysis-prediction-job]');previousJob.hidden=true;previousJob.replaceChildren();
    result=response.result || response;
    selectedDate='';
    applyAnalysisResult(host,result);
    appliedInputs=inputKey();updatePredictionControls();
    draw();events();
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const errors=host.querySelector('[data-page-error]');errors.replaceChildren();
    try{
      const symbol=symbolsFrom(formValue(form,'symbol'),1)[0],start=formValue(form,'start'),end=formValue(form,'end');validateDates(start,end);
      const ticket=++analysisSequence,requestedInputs=inputKey();
      const response=await busy(form.querySelector('[type=submit]'),()=>api.analyze({symbol,start:start || null,end,analysis_method:'structure'},signal),'正在分析…');
      if(signal.aborted || ticket!==analysisSequence || requestedInputs!==inputKey())return;
      showResult(response);
      notify('已读取本次结构与量价分析');
    }catch(error){showError(host,error);}
  },{signal});
  async function showPrediction(response,ticket,version){
    if(signal.aborted || ticket!==predictionSequence || version!==analysisVersion || !inputsCurrent())return;
    const value=response.result || response,reference=predictionReference(value);
    let payload=value.chart,error;
    try{if(!payload && reference)payload=await api.predictionChart(reference,signal,{predictionRunId:value.lake_run_id});}
    catch(failure){if(failure.name==='AbortError')return;error=failure;}
    if(signal.aborted || ticket!==predictionSequence || version!==analysisVersion || !inputsCurrent())return;
    const quality=predictionQuality(value,{chart:payload,error}),compatibility=analysisPredictionCompatible(result,value,payload);
    const horizon=Number(value.parameters?.horizon ?? value.identity?.parameters?.horizon ?? payload?.parameters?.horizon);
    const currentHorizon=Number(predictionPanel.querySelector('[data-prediction-horizon]').value);
    if(horizon!==currentHorizon){predictionState.innerHTML='<p class="notice">预测周期已改变，旧返回未叠加当前图。</p>';return;}
    predictionRecord=compatibility.compatible && !['failed','unavailable'].includes(quality.state)?{result:value,chart:payload}:null;
    const state=!compatibility.compatible && quality.state==='usable'?{state:'degraded',label:'降级参考',reason:compatibility.reason}:quality;
    const runId=value.lake_run_id || value.run_id || '';
    predictionState.innerHTML=`<div class="notice" data-prediction-quality="${state.state}"><strong>预测质量：${escapeHtml(state.label)}</strong><p>${escapeHtml(state.reason)}${!compatibility.compatible?`；未拼接：${escapeHtml(compatibility.reason)}`:''}</p><p>中位数期末变化 ${percent(value.summary?.terminal_return_median)} · 上涨采样比例 ${percent(value.summary?.up_path_fraction)}（不是校准概率）</p>${runId?`<a href="${escapeHtml(routeLink('analysis',{report:result.lake_run_id || result.run_id,prediction:runId}))}">重开本次分析与预测</a>`:''}${disclosure('预测输入、参数与绑定身份',{quality:value.quality,parameters:value.identity?.parameters || value.parameters,binding:value.analysis_binding,history_verification:payload?.history_verification,prediction_ref:reference})}</div>`;
    draw();
  }
  function predictionCompletion(ticket,version){return async job=>{
    if(signal.aborted || ticket!==predictionSequence || version!==analysisVersion)return;
    if(job.result)await showPrediction(job.result,ticket,version);
    else if(job.status==='failed'){predictionRecord=null;predictionState.innerHTML=`<div class="notice error" data-prediction-quality="failed">预测质量：失败 · ${escapeHtml(job.error || '任务执行失败')}</div>`;draw(false);}
    else if(job.status==='cancelled'){predictionRecord=null;predictionState.innerHTML='<p class="notice">本次任务已取消；未将旧结果作为本次预测。</p>';draw(false);}
  };}
  predictionButton.addEventListener('click',async()=>{
    if(!inputsCurrent())return;
    try{
      const spec=analysisPredictionSpec(result,predictionPanel.querySelector('[data-prediction-horizon]').value),ticket=++predictionSequence,version=analysisVersion;
      predictionWatcher?.stop();predictionRecord=null;predictionState.innerHTML='<p class="form-note">预测任务状态见下方；质量结论等待保存结果。</p>';draw(false);
      const target=predictionPanel.querySelector('[data-analysis-prediction-job]');target.hidden=false;
      predictionWatcher=await submitJob(target,predictionButton,'prediction',spec,{signal,onComplete:predictionCompletion(ticket,version)});
      updatePredictionControls();
    }catch(error){showError(host,error);}
  },{signal});
  predictionPanel.querySelector('[data-prediction-layer]').addEventListener('change',()=>draw(false),{signal});
  predictionPanel.querySelector('[data-prediction-horizon]').addEventListener('change',()=>{++predictionSequence;predictionRecord=null;predictionState.innerHTML='<p class="form-note">预测周期已修改；显式运行后读取对应结果。</p>';draw(false);},{signal});
  for(const field of form.querySelectorAll('input'))field.addEventListener('input',()=>{++predictionSequence;predictionRecord=null;predictionState.replaceChildren();updatePredictionControls();draw(false);},{signal});
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
    try{const response=await api.report(params.get('report'),signal);if(!signal.aborted)showResult(response);}catch(error){showError(host,error);}
  }
  if(result && params.get('prediction')){
    const ticket=++predictionSequence,version=analysisVersion;
    try{const value=await api.report(params.get('prediction'),signal);const record=value.result || value;const horizon=record.identity?.parameters?.horizon || record.parameters?.horizon;if([1,5].includes(Number(horizon)))predictionPanel.querySelector('[data-prediction-horizon]').value=String(horizon);await showPrediction(value,ticket,version);}catch(error){if(error.name!=='AbortError')showError(host,error);}
  }else if(result && params.get('prediction_job')){
    try{const job=await api.job(params.get('prediction_job'),signal),spec=job.params || job.spec || {};if(job.kind!=='prediction' || spec.analysis_run_id!==(result.lake_run_id || result.run_id) || spec.symbol!==result.symbol || spec.end!==day(result.as_of || result.data_end))throw new Error('所选预测任务与当前分析身份不一致。');if([1,5].includes(Number(spec.horizon)))predictionPanel.querySelector('[data-prediction-horizon]').value=String(spec.horizon);const ticket=++predictionSequence,version=analysisVersion;const target=predictionPanel.querySelector('[data-analysis-prediction-job]');target.hidden=false;predictionWatcher=watchJob(target,job,{signal,onComplete:predictionCompletion(ticket,version)});}catch(error){if(error.name!=='AbortError')showError(host,error);}
  }
  updatePredictionControls();
  return()=>{chart?.destroy();featureCleanup?.();predictionWatcher?.stop();};
}
