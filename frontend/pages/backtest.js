import {api} from '../core/api.js';
import {arrayOf,badge,busy,disclosure,empty,errorAdvice,errorHtml,escapeHtml,formValue,heading,nextNaturalDay,notify,number,showError,symbolsFrom,table,today,validateDates,yearStart} from '../core/ui.js';
import {submitJob} from '../core/jobs.js';
import {renderReport,reportList} from '../core/reports.js';
import {bindModelSelector,seriesOptions} from '../core/models.js';
import {releaseSummary} from '../core/study.js';

export function backtestSpec(form){
  const mode=formValue(form,'mode'),start=formValue(form,'start'),end=formValue(form,'end');validateDates(start,end);
  const spec={mode,start,end,top_k:Number(formValue(form,'top_k')),initial_cash:Number(formValue(form,'initial_cash'))};
  const releaseId=formValue(form,'release_id')||'active',universeScope=formValue(form,'universe_scope');
  if(mode==='historical'){const study_id=formValue(form,'study_id');return {...spec,...(study_id?{study_id}:{release_id:releaseId})};}
  if(['frozen','research_replay','watchlist_replay'].includes(mode)){
    const scope=formValue(form,'scope') || 'training';
    if(mode==='watchlist_replay'&&scope!=='symbols')throw new Error('关注池历史权重回放要求明确指定股票池（关注页勾选/全选）。');
    if(scope!=='training'){spec.scope=scope;spec.symbols=scope==='all'?[]:symbolsFrom(formValue(form,'symbols'),6000);}
    if(scope==='symbols'&&universeScope)spec.universe_scope=universeScope;
    if(mode==='watchlist_replay')return {...spec,release_id:releaseId};
    if(formValue(form,'version_source')==='active')return {...spec,release_id:releaseId};
  }
  const key=mode==='rolling'?'series_id':'model_id',value=formValue(form,key);if(!value)throw new Error(mode==='rolling'?'请选择滚动模型序列。':'请选择冻结模型版本。');spec[key]=value;return spec;
}

// Read-only effective-pool preview body; the endpoint accepts frozen|watchlist_replay and at most 200 symbols.
export function backtestPreviewBody(form){
  const mode=formValue(form,'mode');if(!['frozen','watchlist_replay'].includes(mode))throw new Error('检查股票池只支持固定模型或关注池历史回放，请先调整回测方式。');
  const scope=formValue(form,'scope') || 'training';
  if(scope==='training')throw new Error('检查股票池需要先选择明确指定股票池或全部沪深 A 股。');
  const start=formValue(form,'start'),end=formValue(form,'end');validateDates(start,end);
  const releaseId=formValue(form,'release_id')||'active';
  if(mode==='watchlist_replay'&&scope!=='symbols')throw new Error('关注池历史权重回放要求明确指定股票池。');
  return {mode,release_id:releaseId,expected_release_id:releaseId==='active'?'':releaseId,scope:scope==='all'?'all':'symbols',symbols:scope==='all'?[]:symbolsFrom(formValue(form,'symbols'),200),start,end,universe_scope:formValue(form,'universe_scope')||'release'};
}

// Watchlist-replay submissions must stay inside the saved out-of-sample series window.
export function replayRangeError(spec,seriesRange){
  if(!spec||spec.mode!=='watchlist_replay'||!seriesRange||!seriesRange.start||!seriesRange.end)return '';
  if(spec.start<seriesRange.start)return `关注池历史回放开始日期 ${spec.start} 早于原序列起点 ${seriesRange.start}；请改为 ${seriesRange.start} 或更晚。`;
  if(spec.end>seriesRange.end)return `关注池历史回放结束日期 ${spec.end} 晚于原序列上限 ${seriesRange.end}；请改为 ${seriesRange.end} 或更早。`;
  return '';
}

// Read-only coverage payload -> latest verified daily-bar session, or '' when unknown.
export function latestVerifiedSession(coverage){
  const rows=Array.isArray(coverage?.datasets)?coverage.datasets:null;
  const daily=rows?rows.find(row=>row && row.name==='daily_bars'):coverage?.datasets?.daily_bars;
  const day=String(daily?.complete_through || daily?.watermark || coverage?.target_date || '').slice(0,10);
  return /^\d{4}-\d{2}-\d{2}$/.test(day)?day:'';
}

// Name the saved-series boundary and the entries that can reach a newer verified session.
export function replaySeriesHint(spec,seriesRange,latestSession=''){
  if(!spec||spec.mode!=='watchlist_replay'||!seriesRange||!seriesRange.end)return '';
  const end=String(seriesRange.end).slice(0,10),start=String(seriesRange.start||'').slice(0,10),latest=String(latestSession||'').slice(0,10);
  const where=start?`${start} — ${end}`:end;
  if(/^\d{4}-\d{2}-\d{2}$/.test(latest)&&latest>end)return `行情已核验到 ${latest}，但长区间回放只重放已保存的样本外序列（${where}），不会把 ${latest} 现场补算进去。要看 ${latest} 当天结果请用「固定模型」短区间（默认权重）；要让长区间包含 ${latest}，需重跑十年研究协议并把请求截止设为 ${latest}。`;
  return `长区间回放只重放已保存的样本外序列（${where}），不现场补算之后更新的交易日。要看最新交易日请用「固定模型」短区间（默认权重）；要让长区间包含新日期，需重跑十年研究协议并把请求截止设为最新交易日。`;
}

export function backtestPreviewHtml(value,request={}){
  const info=value?.window || {},counts=value?.counts || {},rows=arrayOf(value,'per_symbol'),warnings=arrayOf(value,'warnings'),blockers=arrayOf(value,'blockers');
  const replay=(value?.mode || request.mode)==='watchlist_replay';
  const requestedScope=value?.requested_universe_scope || request.universe_scope || value?.universe_scope;
  const effectiveScope=value?.effective_universe_scope || value?.universe_scope || requestedScope;
  const scopeLabel=scope=>escapeHtml(({explicit_symbols:'显式股票池',release:'默认方法资格股票池',saved_evaluation:'历史评价序列'})[scope] || scope || '未返回');
  const metrics=[['请求区间',`${escapeHtml(info.requested_start || request.start || '—')} — ${escapeHtml(info.requested_end || request.end || '—')}`],['最早可回测日',escapeHtml(info.earliest_start || '—')],['实际首末交易日',`${escapeHtml(info.first_session || '—')} — ${escapeHtml(info.last_session || '—')}`],['可用交易日',number(info.available_sessions,0)],['请求股票池',scopeLabel(requestedScope)],['实际交易股票池',scopeLabel(effectiveScope)]];
  if(replay)metrics.push(['历史评价序列来源',scopeLabel(value?.evaluation_universe_scope)]);
  else metrics.push(['资格上限',number(value?.cap,0)]);
  const statusLabels={eligible:'合格',excluded:'被排除',available:'历史序列可用',unavailable:replay?'历史序列不可用':'不可用',unknown:'未知'};
  const columns=[{label:'股票',render:row=>`<span class="code-key">${escapeHtml(row.symbol)}</span>`},{label:replay?'历史序列可用性':'资格状态',render:row=>badge(row.status,statusLabels[row.status] || row.status || '未知')},{label:'原因',render:row=>escapeHtml(row.reason || '—'),wrap:true}];
  if(!replay)columns.push({label:'排名',render:row=>row.rank===null||row.rank===undefined?'—':number(row.rank,0)},{label:'60日成交额中位',render:row=>number(row.amount60_median)},{label:'覆盖率',render:row=>`252日 ${row.coverage252===null||row.coverage252===undefined?'—':`${number(Number(row.coverage252)*100,1)}%`} · 60日 ${row.coverage60===null||row.coverage60===undefined?'—':`${number(Number(row.coverage60)*100,1)}%`}`});
  const summary=replay?`历史序列可用 ${number(counts.available ?? counts.eligible,0)} / 不可用 ${number(counts.unavailable ?? counts.excluded,0)}`:value?.eligibility_evaluated===false?'资格待核验':`合格 ${number(counts.eligible,0)} / 排除 ${number(counts.excluded,0)}`;
  return `<div class="notice space-top">股票池预检（只读）：${badge(value?.status)} 请求 ${number(counts.requested,0)} 只 / ${summary}${value?.read_ref?` · 读取身份 <span class="code-key">${escapeHtml(value.read_ref)}</span>`:''}</div>${replay?'<p class="form-note">本次仅核对已保存历史评价序列的可用性，未重新核验当前股票池资格；用户关注池限制不改变原历史序列来源。</p>':''}<div class="metrics" style="margin-top:0">${metrics.map(([label,text])=>`<div class="metric"><div class="metric-label">${label}</div><div class="metric-value">${text}</div></div>`).join('')}</div>${warnings.length?`<div class="notice">警告：${warnings.map(item=>escapeHtml(item)).join('；')}</div>`:''}${blockers.length?`<div class="notice error" role="alert">阻断：${blockers.map(item=>escapeHtml(item)).join('；')}</div>`:''}${table(columns,rows,{emptyTitle:'预检没有返回逐股状态',emptyDescription:'检查日期与股票池后重试；预检不提交任务。'})}<p class="form-note">预检只读，不创建运行目录，也不会提交回测任务；实际交易日与资格仍以后端运行结果为准。</p>`;
}

export async function mount(host,{signal,params}){
  let watcher,reportCleanup,cutoffValue='';
  host.innerHTML=heading('模型策略回测','默认一条路径：用默认方法在真实区间上回测，结果由真实 Broker / Ledger 计算。')+`<section class="panel"><div class="panel-header"><h2>运行默认方法回测</h2></div><form id="backtest-form" class="form-toolbar"><input type="hidden" name="release_id" value="${escapeHtml(params.get('release_id') || '')}"><div class="form-grid"><label>回测方式<select name="mode"><option value="frozen">固定模型</option><option value="rolling">滚动模型序列</option></select></label><label class="span-two" data-frozen-field>冻结模型版本<select name="model_id" data-model-select><option value="">正在读取模型…</option></select></label><label class="span-two" data-rolling-field hidden>滚动序列<select name="series_id" data-series-select><option value="">正在读取序列…</option></select></label><label data-backtest-scope-field>固定回测股票池<select name="scope"><option value="training">模型原训练股票池</option><option value="symbols">明确指定股票池</option><option value="all">全部沪深 A 股</option></select></label><label class="span-two" data-backtest-symbols-field hidden>股票代码<input name="symbols" value="${escapeHtml(params.get('symbols') || '')}" placeholder="600519.SH,000001.SZ"></label><label>开始日期<input type="date" name="start" value="${escapeHtml(params.get('start') || yearStart())}" required></label><label>结束日期<input type="date" name="end" value="${escapeHtml(params.get('end') || today())}" required></label><label>组合 Top K<input name="top_k" type="number" min="1" max="100" value="10" required></label><label>初始资金（元）<input name="initial_cash" type="number" min="10000" max="100000000" value="1000000" required></label><button type="submit" class="primary">运行模型回测</button></div><p class="form-note">固定回测受模型信息截止与冻结时间限制。滚动回测按已保存的窗口模型与信息截止逐段运行；不会在回测期间重训或偷用未来模型。</p><p class="form-note" data-backtest-date-note></p><div data-model-identity></div><div data-series-identity></div></form><div class="job-inline" data-backtest-job hidden></div></section><section class="space-top" data-backtest-result>${empty('尚未选择模型回测结果','在模型中心选择冻结版本或滚动序列，然后配置日期和资金。')}</section><div class="section-divider"><h2>已保存模型回测</h2><button class="small-button" data-refresh-reports>刷新索引</button></div><section class="panel" data-saved-backtests></section>`;
  const form=host.querySelector('#backtest-form');
  form.querySelector('[name=mode]').insertAdjacentHTML('afterbegin','<option value="historical">历史滚动重建（阶段协议）</option><option value="research_replay">当前权重历史研究回放</option><option value="watchlist_replay">关注池历史权重回放（长区间）</option>');
  form.querySelector('.form-grid').insertAdjacentHTML('afterbegin',`<label data-version-source>权重来源<select name="version_source"><option value="active">阶段默认研究版本</option><option value="frozen">指定冻结模型</option></select></label><label data-study-source>历史研究ID（可选）<input name="study_id" value="${escapeHtml(params.get('study_id') || '')}" placeholder="留空使用阶段默认协议"></label><label data-backtest-universe-field hidden>股票池口径<select name="universe_scope"><option value="release">按默认方法资格（可能受 1000 只上限截断）</option><option value="explicit_symbols">显式股票池（跳过 1000 只排名截断）</option></select></label>`);
  form.insertAdjacentHTML('beforeend','<div data-backtest-release></div><p class="form-note" data-backtest-mode-note></p>');form.querySelector('.form-note').textContent='历史滚动会按协议补建过去权重；固定模型、已保存滚动序列与当前权重研究回放分别保留时序边界。';
  form.querySelector('[name=version_source]').value=params.get('model_id')?'frozen':(params.get('version_source')==='frozen'?'frozen':'active');form.querySelector('[name=scope]').value=params.get('symbols')?'symbols':'training';const requestedMode=params.has('series_id')?'rolling':params.get('mode') || (params.has('model_id')?'frozen':'historical');form.querySelector('[name=mode]').value=['frozen','rolling','historical','research_replay','watchlist_replay'].includes(requestedMode)?requestedMode:'historical';form.querySelector('[name=universe_scope]').value=['release','explicit_symbols'].includes(params.get('universe_scope'))?params.get('universe_scope'):(params.has('symbols')?'explicit_symbols':'release');
  // Default view: one action plus the actual interval. Every selector/evidence block lives in the advanced disclosure.
  const grid=form.querySelector('.form-grid');
  const startField=grid.querySelector('[name=start]').closest('label');
  const endField=grid.querySelector('[name=end]').closest('label');
  const submitButton=grid.querySelector('button[type=submit]');
  const methodBox=document.createElement('div');
  methodBox.className='run-default';
  methodBox.innerHTML='<span>默认方法</span><strong data-backtest-default-name>读取中…</strong><code data-backtest-default-id>—</code>';
  submitButton.textContent='运行默认方法回测';
  submitButton.setAttribute('aria-label','使用默认方法运行回测');
  submitButton.setAttribute('title','用已发布的默认方法回测；区间取已保存滚动序列的实际区间，可在高级区调整');
  submitButton.classList.add('run-submit');
  const previewButton=document.createElement('button');
  previewButton.type='button';previewButton.className='small-button';previewButton.dataset.backtestPreview='';previewButton.textContent='检查股票池';
  const simple=document.createElement('div');
  simple.className='run-simple';
  simple.append(methodBox,startField,endField,submitButton,previewButton);
  grid.before(simple);
  const mainNote=form.querySelector('.form-note');
  const dateNote=form.querySelector('[data-backtest-date-note]');
  dateNote.className='run-date-note';
  dateNote.textContent='正在读取默认方法与实际区间…';
  simple.append(dateNote);
  const replayHint=document.createElement('p');
  replayHint.className='run-date-note';replayHint.dataset.backtestReplayHint='';replayHint.hidden=true;
  simple.append(replayHint);
  const poolNote=document.createElement('p');
  poolNote.className='form-note';poolNote.dataset.backtestPoolNote='';
  simple.append(poolNote);
  const previewBox=document.createElement('div');previewBox.dataset.backtestPreviewResult='';simple.append(previewBox);
  let seriesRange=null,latestSession='',latestRequested=false;
  // The long replay never extends into a newer lake session; say so next to the dates instead of only failing on submit.
  function updateReplayHint(){
    const replay=formValue(form,'mode')==='watchlist_replay',range=seriesRange;
    replayHint.hidden=!(replay&&range&&range.end);
    replayHint.textContent=replayHint.hidden?'':replaySeriesHint({mode:'watchlist_replay'},range,latestSession);
    if(replay&&range&&range.end&&!latestRequested){
      latestRequested=true;
      api.coverage({},signal).then(value=>{if(signal.aborted)return;latestSession=latestVerifiedSession(value);updateReplayHint();}).catch(()=>{});
    }
  }
  function updatePoolNote(){
    const mode=formValue(form,'mode'),fixed=!['rolling','historical'].includes(mode),scope=formValue(form,'scope'),universeScope=formValue(form,'universe_scope');
    if(!fixed){poolNote.textContent='滚动或历史重建回测使用模型原训练股票池，可能买入关注池之外的股票。';return;}
    if(scope==='training'){poolNote.textContent='当前回测使用模型原训练股票池，可能买入关注池之外的股票。';return;}
    if(scope==='all'){poolNote.textContent='本次回测使用全部沪深 A 股股票池，可能买入关注池之外的股票。';return;}
    let pool=[];try{pool=symbolsFrom(formValue(form,'symbols'),6000);}catch{poolNote.textContent='明确指定股票池尚未填写或格式有误；请填写股票代码，或回到「我的关注」全选后带入。';return;}
    const scopeText=universeScope==='explicit_symbols'?'显式股票池（跳过 1000 只排名截断）':'按默认方法股票池资格（可能受 1000 只上限截断）';
    poolNote.textContent=`本次回测股票池：关注所选 ${pool.length} 只（${pool.join('、')}）；${scopeText}；只在这些股票内买卖。`;
  }
  // One derivation for both the model selector and the active default release: start = next natural day after the cutoff.
  function applyDerivedStart(cutoff){
    const cut=String(cutoff || '').slice(0,10),day=nextNaturalDay(cut);if(!day)return '';
    const field=form.querySelector('[name=start]');
    if(params.has('start')||field.dataset.edited==='true')return field.value;
    if(field.value && field.value!==yearStart() && field.dataset.suggested!=='true')return field.value;
    field.value=day;field.dataset.suggested='true';
    const end=formValue(form,'end'),note=host.querySelector('[data-backtest-date-note]');
    if(note)note.textContent=`开始日期已建议为默认方法信息截止 ${cut} 的下一自然日 ${day}；实际可用交易日仍由后端交易日历核验。${end&&day>end?` 但该建议晚于结束日期 ${end}，请调整结束日期或手动填写开始日期。`:''}`;
    return day;
  }
  const advanced=document.createElement('details');
  advanced.className='advanced-panel';
  advanced.innerHTML='<summary>高级：回测方式、版本与股票池</summary><div class="advanced-body"></div>';
  grid.before(advanced);
  advanced.querySelector('.advanced-body').append(grid,form.querySelector('[data-model-identity]'),form.querySelector('[data-series-identity]'),form.querySelector('[data-backtest-release]'),mainNote,form.querySelector('[data-backtest-mode-note]'));
  function modeChanged(){
    if(formValue(form,'mode')==='watchlist_replay'&&formValue(form,'version_source')!=='active')form.querySelector('[name=version_source]').value='active';
    const mode=formValue(form,'mode'),rolling=mode==='rolling',historical=mode==='historical',replay=mode==='watchlist_replay',explicit=formValue(form,'scope')==='symbols',fixed=!rolling&&!historical,active=formValue(form,'version_source')==='active';
    host.querySelector('[data-frozen-field]').hidden=!fixed||active;host.querySelector('[data-rolling-field]').hidden=!rolling;host.querySelector('[data-model-identity]').hidden=!fixed||active;host.querySelector('[data-series-identity]').hidden=!rolling;host.querySelector('[data-backtest-scope-field]').hidden=!fixed;host.querySelector('[data-backtest-symbols-field]').hidden=!fixed||!explicit;host.querySelector('[data-backtest-universe-field]').hidden=!fixed||!explicit;host.querySelector('[data-version-source]').hidden=!fixed||replay;host.querySelector('[data-study-source]').hidden=!historical;host.querySelector('[data-backtest-release]').hidden=rolling||(fixed&&!active);form.querySelector('[name=symbols]').required=fixed&&explicit;form.querySelector('[name=symbols]').disabled=!fixed||!explicit;previewButton.hidden=!(mode==='frozen'||replay);
    const range=seriesRange?`${seriesRange.start||'—'} — ${seriesRange.end||'—'}（${number(seriesRange.folds,0)} 折）`:'原序列区间未返回';
    host.querySelector('[data-backtest-mode-note]').textContent=historical?'逐折只用当时之前的数据拟合，必要时补建历史权重。协议为今天选出，存在协议事后选择偏差；训练史不足会明确返回可用起点。':replay?`关注池历史权重回放：读取已保存滚动序列各折冻结权重，区间须落在 ${range} 内；评价人口被用户关注池事后限制，不是原全市场样本外成绩。`:mode==='research_replay'?'研究回放：今天权重可查看训练前历史，但属于样本内/事后重建，不计入样本外排名。':rolling?'重放已保存的样本外窗口，不自动补建缺失折。':'固定权重仅允许信息截止后的区间；最新重拟合没有独立历史成绩。';
    if(replay&&seriesRange?.end){const endField=form.querySelector('[name=end]');if(endField.dataset.edited!=='true'&&endField.value>seriesRange.end)endField.value=seriesRange.end;dateNote.textContent=`回放请求区间：${formValue(form,'start')} — ${endField.value}；原序列可用区间：${seriesRange.start} — ${seriesRange.end}（${number(seriesRange.folds,0)} 折）。实际交易日由后端日历核验。`;}
    updatePoolNote();updateReplayHint();}
  form.querySelector('[name=version_source]').addEventListener('change',modeChanged,{signal});
  modeChanged();form.querySelector('[name=mode]').addEventListener('change',modeChanged,{signal});form.querySelector('[name=scope]').addEventListener('change',modeChanged,{signal});form.querySelector('[name=universe_scope]').addEventListener('change',modeChanged,{signal});
  form.querySelector('[name=start]').addEventListener('input',event=>{event.target.dataset.edited='true';if(formValue(form,'mode')==='watchlist_replay')modeChanged();},{signal});
  form.querySelector('[name=end]').addEventListener('input',event=>{event.target.dataset.edited='true';if(formValue(form,'mode')==='watchlist_replay')modeChanged();},{signal});
  form.querySelector('[name=symbols]').addEventListener('input',updatePoolNote,{signal});
  function modelChanged(model){if(!model || formValue(form,'mode')!=='frozen')return;cutoffValue=String(model.information_cutoff || '').slice(0,10)||cutoffValue;applyDerivedStart(model.information_cutoff);}
  async function showSeries(id){if(!id){host.querySelector('[data-series-identity]').replaceChildren();return;}try{const series=await api.learningSeries(id,signal);if(signal.aborted)return;const starts=(series.members || []).map(row=>row.start).filter(Boolean).sort(),ends=(series.members || []).map(row=>row.end).filter(Boolean).sort();const rawEnd=ends.at(-1),end=rawEnd && rawEnd>today()?today():rawEnd;if(starts.length)form.querySelector('[name=start]').value=starts[0];if(end)form.querySelector('[name=end]').value=end;host.querySelector('[data-series-identity]').innerHTML=`<dl class="detail-list"><div><dt>原样本外区间</dt><dd>${escapeHtml(starts[0] || '—')} — ${escapeHtml(ends.at(-1) || '—')}</dd></div><div><dt>冻结窗口</dt><dd>${number(series.members?.length,0)}</dd></div></dl>${disclosure('已保存的滚动模型序列',series)}`;const note=host.querySelector('[data-backtest-date-note]');if(note)note.textContent=`实际区间：${starts[0] || '—'} — ${end || '—'}（来自已保存滚动序列，${number(series.members?.length,0)} 折）；结束日不超过今天，交易日仍由后端日历核验。`;}catch(error){showError(host,error);}}
  function showResult(value){reportCleanup?.();const result=value?.result || value;reportCleanup=renderReport(host.querySelector('[data-backtest-result]'),result,{title:'模型策略回测'});if(result.mode==='watchlist_replay')host.querySelector('[data-backtest-result]').insertAdjacentHTML('afterbegin','<div class="notice space-top">关注池历史权重回放：权重取自已保存滚动序列的逐折冻结模型；评价人口被用户关注池事后限制，不是原全市场样本外成绩。</div>');else if(result.mode==='research_replay')host.querySelector('[data-backtest-result]').insertAdjacentHTML('afterbegin','<div class="notice space-top">当前权重历史研究回放，包含样本内或事后重建，不能作为独立样本外成绩。</div>');}
  async function openReport(id){try{const result=await api.report(id,signal);if(!signal.aborted)showResult(result);}catch(error){showError(host,error);}}
  function advise(error){const advice=errorAdvice(error?.message || String(error || ''),{cutoff:cutoffValue});if(advice)notify(advice,'error');return advice;}
  async function previewPool(){
    const button=host.querySelector('[data-backtest-preview]'),target=host.querySelector('[data-backtest-preview-result]');
    try{const body=backtestPreviewBody(form);const value=await busy(button,()=>api.backtestPreview(body,signal),'检查中…');if(signal.aborted)return;target.innerHTML=backtestPreviewHtml(value,body);}
    catch(error){advise(error);showError(host,error);}
  }
  host.querySelector('[data-backtest-preview]').addEventListener('click',previewPool,{signal});
  form.addEventListener('submit',async event=>{event.preventDefault();try{const spec=backtestSpec(form);const rangeError=replayRangeError(spec,seriesRange);if(rangeError)throw new Error(rangeError);const target=host.querySelector('[data-backtest-job]');target.hidden=false;watcher?.stop();watcher=await submitJob(target,form.querySelector('[type=submit]'),'backtest',spec,{signal,onComplete:async job=>{if(job.result)showResult(job.result);else if(job.run_id)await openReport(job.run_id);if(job.error){const error=new Error(job.error);showError(host,error);advise(error);}await loadReports();}});}catch(error){advise(error);showError(host,error);}},{signal});
  async function loadReports(){try{const response=await api.reports(signal);if(signal.aborted)return;host.querySelector('[data-saved-backtests]').innerHTML=reportList(arrayOf(response,'reports').filter(row=>row.family==='model_backtest_v1'||['model_backtest','backtest','simulation'].includes(row.kind)),{limit:30});}catch(error){if(!signal.aborted)host.querySelector('[data-saved-backtests]').innerHTML=errorHtml(error);}}
  async function loadModels(){try{const catalog=await bindModelSelector(host,{signal,selected:params.get('model_id') || '',onChange:modelChanged});if(signal.aborted)return;host.querySelector('[data-series-select]').innerHTML=seriesOptions(catalog.series,params.get('series_id') || '');const chosen=host.querySelector('[data-model-select]')?.value;if(chosen)modelChanged(catalog.models.find(row=>row.model_id===chosen));if(params.get('series_id'))await showSeries(params.get('series_id'));}catch(error){showError(host,error);}}
  host.querySelector('[data-series-select]').addEventListener('change',event=>showSeries(event.target.value),{signal});
  host.querySelector('[data-refresh-reports]').addEventListener('click',loadReports,{signal});host.querySelector('[data-saved-backtests]').addEventListener('click',event=>{const button=event.target.closest('[data-report-id]');if(button)openReport(button.dataset.reportId);},{signal});
  async function loadWatchlistSeries(id,{applyRange=false}={}){
    if(!id)return null;
    try{const series=await api.learningSeries(id,signal);if(signal.aborted)return null;const members=arrayOf(series,'members'),starts=members.map(row=>row.start).filter(Boolean).sort(),ends=members.map(row=>row.end).filter(Boolean).sort();seriesRange={start:starts[0] || '',end:ends.at(-1) || '',folds:members.length};if(applyRange){const startField=form.querySelector('[name=start]'),endField=form.querySelector('[name=end]');if(!params.has('start')&&startField.dataset.edited!=='true'&&seriesRange.start)startField.value=seriesRange.start;if(!params.has('end')&&seriesRange.end)endField.value=seriesRange.end>today()?today():seriesRange.end;}modeChanged();return seriesRange;}
    catch(error){if(error.name!=='AbortError'){if(formValue(form,'mode')==='watchlist_replay')dateNote.textContent='原序列区间读取失败，请检查错误后重新读取默认方法。';showError(host,error);}return null;}
  }
  // The concrete release_id from the URL wins over the currently active default; fall back to active if unknown.
  async function resolveRelease(){
    const value=await api.activeRelease(signal);if(signal.aborted)return {release:null,requested:false,matched:false};
    const active=value.release || null,requested=params.get('release_id') || '',wantsConcrete=!!requested&&requested!=='active';
    if(wantsConcrete&&active?.release_id!==requested){
      const all=await api.releases(signal);if(signal.aborted)return {release:null,requested:true,matched:false};
      const found=arrayOf(all,'releases').find(row=>row.release_id===requested);
      return {release:found || active,requested:true,matched:!!found};
    }
    return {release:active,requested:wantsConcrete,matched:!!active};
  }
  async function loadActive(){
    try{
      const resolved=await resolveRelease();if(signal.aborted)return null;
      const release=resolved.release;
      host.querySelector('[data-backtest-release]').innerHTML=releaseSummary(release);
      const nameHost=host.querySelector('[data-backtest-default-name]'),idHost=host.querySelector('[data-backtest-default-id]'),note=host.querySelector('[data-backtest-date-note]'),modeSelect=form.querySelector('[name=mode]');
      if(nameHost)nameHost.textContent=release?.name || '尚无默认方法';
      if(idHost)idHost.textContent=release?`${release.release_id} · 信息截止 ${release.information_cutoff || '—'}`:'请先到模型中心启用默认方法';
      if(!release){const missingReplay=[...modeSelect.options].find(option=>option.value==='watchlist_replay');if(missingReplay){missingReplay.disabled=true;missingReplay.textContent='关注池历史权重回放（需先启用默认方法）';}if(formValue(form,'mode')==='watchlist_replay'){modeSelect.value='research_replay';modeChanged();}if(note)note.textContent='尚无默认方法；可在高级区显式选择模型或历史研究协议，结果不会被当作默认方法。';return null;}
      cutoffValue=String(release.information_cutoff || release.protocol?.information_cutoff || '').slice(0,10);
      const replayOption=[...modeSelect.options].find(option=>option.value==='watchlist_replay');
      if(replayOption){replayOption.disabled=!release.series_id;replayOption.textContent=release.series_id?'关注池历史权重回放（长区间）':'关注池历史权重回放（默认方法无保存序列）';}
      if(formValue(form,'mode')==='watchlist_replay'&&!release.series_id){modeSelect.value='research_replay';modeChanged();if(note)note.textContent='默认方法尚未保存滚动序列，长区间回放已改用当前权重历史研究回放；请到模型中心确认默认方法。';}
      if(release.series_id)await loadWatchlistSeries(release.series_id,{applyRange:formValue(form,'mode')==='watchlist_replay'});
      if(formValue(form,'mode')==='frozen'&&formValue(form,'version_source')==='active'&&!params.has('start'))applyDerivedStart(cutoffValue);
      if(resolved.requested&&!resolved.matched&&note)note.textContent='请求的发布版本未在发布列表中找到，已回退当前默认方法；请回到关注页重新进入。';
      const explicit=params.has('model_id')||params.has('series_id')||params.has('mode')||params.has('study_id')||params.has('release_id');
      if(explicit)return release;
      if(release.series_id){
        modeSelect.value='rolling';modeChanged();
        const seriesSelect=host.querySelector('[data-series-select]');
        if(seriesSelect && [...seriesSelect.options].some(option=>option.value===release.series_id)){seriesSelect.value=release.series_id;await showSeries(release.series_id);}
        else if(note)note.textContent='默认方法尚未返回可用的已保存滚动序列；已保留历史协议回放，可在高级区调整。';
      }else if(note)note.textContent='默认方法未返回滚动序列；默认使用历史协议回放，区间由后端日历核验。';
      return release;
    }catch(error){if(error.name!=='AbortError')showError(host,error);return null;}
  }
  await Promise.all([loadModels(),loadReports()]);
  const release=await loadActive();
  if(!release && !params.has('mode') && !params.has('model_id') && !params.has('series_id')){form.querySelector('[name=mode]').value='historical';modeChanged();}
  if(params.get('report'))await openReport(params.get('report'));
  return()=>{watcher?.stop();reportCleanup?.();};
}
