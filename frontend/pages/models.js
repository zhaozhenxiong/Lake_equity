import {api} from '../core/api.js';
import {arrayOf,badge,bindSymbolSearch,busy,dataTable,disclosure,empty,errorHtml,escapeHtml,formValue,fullReportLink,heading,notify,number,percent,showError,statusChip,symbolsFrom,table,today,validateDates} from '../core/ui.js';
import {submitJob} from '../core/jobs.js';
import {createPredictionChart} from '../core/charts.js';
import {defaultMethodIdentity,modelLinks,modelName,modelSummary} from '../core/models.js';
import {mountStudy} from '../core/study.js';

export function trainingSpec(form){
  const values=new FormData(form),start=formValue(form,'start'),end=formValue(form,'end');validateDates(start,end);
  const scope=formValue(form,'scope') || 'symbols';
  const spec={scope,symbols:scope==='all'?[]:symbolsFrom(formValue(form,'symbols'),6000),start,end,backend:formValue(form,'backend'),factor_groups:values.getAll('factor_groups'),models:values.getAll('models')};
  if(spec.symbols.some(symbol=>!symbol.includes('.')))throw new Error('请明确股票交易所后缀，例如 600519.SH、000001.SZ。');
  if(!spec.factor_groups.length || !spec.models.length)throw new Error('至少选择一个因子组和一个模型。');
  for(const key of ['horizon','train_sessions','valid_sessions','test_sessions','max_folds','top_k','initial_cash','seed'])spec[key]=Number(formValue(form,key));
  if(spec.models.includes('mlp')){spec.model_options={device:formValue(form,'device')};for(const key of ['batch_size','max_epochs','patience','learning_rate','weight_decay','dropout','cpu_threads'])spec.model_options[key]=Number(formValue(form,key));}
  return spec;
}

export async function mount(host,{signal,params}) {
  let trainingWatcher,predictionWatcher,refitWatcher,predictionChart,studyCleanup,showArchived=false,archivePlan=null,learningRuns=[],learningRunsExpanded=false;
  host.innerHTML=heading('模型中心','默认只保留一个方法；训练、对比与历史实验收在高级区，不改变默认方法。')+`<div class="segmented" role="tablist" aria-label="模型中心视图"><button role="tab" data-model-tab="training" aria-selected="true">训练与模型版本</button><button role="tab" data-model-tab="prediction" aria-selected="false">Kronos 预测</button></div><div data-training-section><section class="panel space-top"><div class="panel-header"><div><h2>统一多因子训练</h2><p>默认量价 38 项 + 融资融券 5 项；均线输入包含在同一特征合同内</p></div></div><div class="panel-body"><form id="factor-training-form" class="form-grid"><label>训练范围<select name="scope"><option value="symbols">指定股票池</option><option value="all">全部沪深 A 股</option></select></label><label class="span-two">训练股票池（1–6000 只；全市场按实际人口）<textarea name="symbols" rows="3" placeholder="600519.SH,000001.SZ,600000.SH" required></textarea></label><label>起始日期<input name="start" type="date" value="2024-01-01" required></label><label>结束日期<input name="end" type="date" value="${today()}" required></label><label>学习后端<select name="backend"><option value="qlib">Qlib 独立环境</option><option value="local">本地同合同验证</option></select></label><label>收益窗口（交易日）<input name="horizon" type="number" value="5" min="1" max="20" required></label><label>训练期（交易日）<input name="train_sessions" type="number" value="252" min="40" max="1260" required></label><label>验证期（交易日）<input name="valid_sessions" type="number" value="63" min="10" max="252" required></label><label>测试期（交易日）<input name="test_sessions" type="number" value="63" min="10" max="252" required></label><label>最多滚动窗口<input name="max_folds" type="number" value="3" min="1" max="12" required></label><label>组合 Top K<input name="top_k" type="number" value="10" min="1" max="100" required></label><label>初始资金（元）<input name="initial_cash" type="number" value="1000000" min="10000" max="100000000" required></label><label>随机种子<input name="seed" type="number" value="42" min="0" max="2147483647" required></label><fieldset class="span-two model-checks"><legend>因子组</legend><label><input name="factor_groups" type="checkbox" value="price_volume" checked>量价与均线（38 项）</label><label><input name="factor_groups" type="checkbox" value="margin" checked>融资融券（5 项）</label><label><input name="factor_groups" type="checkbox" value="money_flow">资金流（按真实可得时间）</label></fieldset><fieldset class="span-two model-checks"><legend>模型</legend><label><input name="models" type="checkbox" value="ridge" checked>Ridge</label><label><input name="models" type="checkbox" value="lightgbm" checked>LightGBM</label><label><input name="models" type="checkbox" value="mlp" checked>均线多因子 MLP</label></fieldset><details class="span-all"><summary>MLP 训练参数</summary><div class="form-grid space-top"><label>设备<select name="device"><option value="cpu">CPU</option><option value="cuda">CUDA</option></select></label><label>最大轮数<input name="max_epochs" type="number" min="1" max="100" value="50" required></label><label>早停耐心<input name="patience" type="number" min="1" max="20" value="6" required></label><label>批大小<input name="batch_size" type="number" min="1" max="8192" value="2048" required></label><label>学习率<input name="learning_rate" type="number" min="0.000001" max="0.01" step="0.000001" value="0.001" required></label><label>权重衰减<input name="weight_decay" type="number" min="0" max="1" step="0.0001" value="0.001" required></label><label>Dropout<input name="dropout" type="number" min="0" max="0.5" step="0.05" value="0.1" required></label><label>CPU 线程<input name="cpu_threads" type="number" min="1" max="4" value="4" required></label></div></details><div class="button-row span-all"><button type="button" data-learning-preflight>检查数据与运行条件</button><button type="submit" class="primary">训练并保存模型版本</button></div></form><p class="form-note">每窗口仅训练期拟合处理器。提交前核对覆盖；缺失特征和设备不可用会明确返回原因。训练完成不自动选入策略。</p><div data-learning-catalog class="space-top"></div><div data-learning-preflight-result></div></div><div class="job-inline" data-factor-training-job hidden></div><div class="job-inline" data-refit-job hidden></div><div data-learning-report></div></section><div class="section-divider"><h2>冻结模型版本</h2><button class="small-button" data-refresh-models>刷新版本</button></div><section class="panel"><div class="panel-body model-grid" data-model-list></div><div data-model-detail></div></section><div class="section-divider"><h2>已保存的训练与滚动实验</h2><span class="small muted">默认显示最近 5 条，可按需展开</span><button class="small-button" data-refresh-learning>刷新报告</button></div><section class="panel"><div data-learning-runs class="panel-body"></div></section></div><section class="panel space-top" data-prediction-section hidden><div class="panel-header"><h2>Kronos 预测</h2><span data-assets-state></span></div><form id="prediction-form" class="form-toolbar"><div class="form-grid"><label>股票代码<input id="prediction-symbol" name="symbol" value="${escapeHtml(params.get('symbol') || '')}" placeholder="600519.SH" required autocomplete="off"></label><label>可知截止<input name="end" type="date" value="${today()}" required></label><label>历史窗口<select name="lookback"><option>128</option><option>256</option></select></label><label>预测交易日<select name="horizon"><option>1</option><option selected>5</option><option>10</option></select></label><label>采样路径<select name="samples"><option>8</option><option>32</option><option>64</option></select></label><label>随机种子<input name="seed" type="number" value="42" min="0" required></label><label>设备<select name="device"><option value="cpu">CPU</option><option value="cuda">CUDA</option><option value="auto">自动</option></select></label><button type="submit" class="primary">运行预测</button></div><p class="form-note">采样路径比例不是校准胜率；预测不自动改变模型版本选择与目标仓位。</p></form><div class="job-inline" data-prediction-job hidden></div><div data-prediction-result>${empty('尚未运行预测','显式提交后读取原预测与核验历史。')}</div></section>`;
  host.querySelector('[data-training-section]').insertAdjacentHTML('afterbegin','<div data-study-center></div>');
  // The default page shows one method + one action; comparison training stays in an explicit advanced disclosure.
  const trainingSection=host.querySelector('[data-training-section]');
  const trainingPanel=trainingSection.querySelector(':scope > section.panel');
  const studyCenter=trainingSection.querySelector('[data-study-center]');
  const advancedDetails=document.createElement('details');
  advancedDetails.className='advanced-panel space-top';
  advancedDetails.innerHTML='<summary>研究对比与训练（高级，不改变默认方法）</summary><div class="advanced-body"></div>';
  trainingPanel.before(advancedDetails);
  advancedDetails.querySelector('.advanced-body').append(studyCenter,trainingPanel);
  advancedDetails.insertAdjacentHTML('beforebegin',`<section class="panel space-top forward-panel" data-forward-panel><div class="panel-header"><div><h2>前瞻验证与 Kronos 互证</h2><p>发布后逐日冻结信号、次日对账；不足样本只报“累计中”，不据此改参数。</p></div><div class="button-row"><span data-forward-chip>${statusChip(null,'读取中')}</span><button type="button" class="small-button" data-refresh-forward aria-label="刷新前瞻账本" title="只读取账本与核验结果，不触发记账或对账">刷新</button></div></div><div class="panel-body" data-forward-body><div class="loading-state">正在读取前瞻账本…</div></div></section>`);
  async function loadForward(){
    const body=host.querySelector('[data-forward-body]'),chip=host.querySelector('[data-forward-chip]');
    if(!body)return;
    try{
      const value=await api.forwardSummary(signal);if(signal.aborted)return;
      body.innerHTML=forwardSummaryBody(value);
      const verified=Number(value?.days?.verified || 0);
      chip.innerHTML=statusChip(verified?'available':'partial',verified?`已核验 ${number(verified,0)} 日`:'待累计');
    }catch(error){
      if(error.name==='AbortError')return;
      if(error.status===404){
        chip.innerHTML=statusChip('neutral','接口未就绪');
        body.innerHTML='<div class="notice"><strong>前瞻接口尚未由当前服务提供</strong><p>重启本地服务后可见；账本已保存在 <span class="code-key">var/learning/forward.db</span>，不会因页面不可见而丢失。</p></div>';
        return;
      }
      chip.innerHTML=statusChip('blocked','读取失败');
      body.innerHTML=errorHtml(error);
    }
  }
  host.querySelector('[data-refresh-forward]').addEventListener('click',loadForward,{signal});
  advancedDetails.insertAdjacentHTML('beforebegin',`<section class="panel space-top default-method-panel" data-default-method-panel><div class="panel-header"><div><h2>默认方法</h2><p>默认页面只用这一版方法：一键流水线、选股与回测都指向它。</p></div><div class="button-row"><span data-default-method-chip>${statusChip(null,'读取中')}</span><button type="button" class="small-button" data-refresh-method aria-label="刷新默认方法" title="重新读取默认方法身份，不启动训练">刷新</button></div></div><div class="panel-body" data-default-method-body><div class="loading-state">正在读取默认方法…</div></div></section><section class="panel space-top archive-panel" data-archive-panel><div class="panel-header"><div><h2>模型归档（减少默认选择面）</h2><p>归档只把旧版本从默认可选列表隐藏，不删除产物与历史报告；默认方法不受影响。</p></div><span data-archive-chip>${statusChip(null,'未预览')}</span></div><div class="panel-body"><div class="button-row"><button type="button" data-archive-preview aria-label="预览模型归档范围" title="预览将保留与归档的模型数量；不会执行归档">预览归档范围</button><button type="button" class="primary" data-archive-confirm aria-label="确认归档预览中的模型" title="按预览计划执行归档；仅隐藏默认可选列表中的旧模型" disabled>确认归档</button></div><p class="form-note">先预览再确认。归档保留全部产物、冻结版本与报告；需要时可勾选“显示归档模型”。</p><div data-archive-result></div></div></section>`);
  const modelPanel=host.querySelector('[data-model-list]').closest('section.panel');
  modelPanel.insertAdjacentHTML('afterbegin','<div class="panel-header"><div><h2>最近冻结版本</h2><p class="model-list-caption" data-model-list-caption>正在读取…</p></div><label class="check-field"><input type="checkbox" data-show-archived aria-label="显示归档模型" title="读取 include_archived=true 的完整模型列表">显示归档模型</label></div>');
  async function loadDefaultMethod(){
    const body=host.querySelector('[data-default-method-body]'),chip=host.querySelector('[data-default-method-chip]');
    try{
      const active=await api.activeRelease(signal);if(signal.aborted)return;
      const method=active?.release || null;
      body.innerHTML=`${defaultMethodIdentity(method,null,{showLink:false})}<div class="button-row space-top"><button type="button" disabled data-refit-default aria-label="更新默认方法权重（暂需在高级区选择训练协议）" title="最终重拟合需要具体训练报告与协议参数；请在高级区的训练报告中执行">更新默认方法权重</button><span class="small muted">一键流水线会自动完成默认方法重拟合；需要指定协议时，在下方高级区的训练报告中选择“按此协议最终重拟合”。</span></div>`;
      chip.innerHTML=statusChip(method?'available':null,method?'默认方法已启用':'尚无默认方法');
    }catch(error){
      if(error.name==='AbortError')return;
      chip.innerHTML=statusChip('blocked','读取失败');
      body.innerHTML=errorHtml(error);
    }
  }
  async function previewArchive(button){
    const target=host.querySelector('[data-archive-result]'),chip=host.querySelector('[data-archive-chip]'),confirm=host.querySelector('[data-archive-confirm]');
    try{
      const value=await busy(button,()=>api.archivePreview(signal),'正在预览…');
      if(signal.aborted)return;
      archivePlan=value?.plan || value || null;
      const total=Number(archivePlan?.total || 0),keep=Number(archivePlan?.keep || 0),archive=Number(archivePlan?.archive || 0),already=Number(archivePlan?.already_archived || 0);
      chip.innerHTML=statusChip(archive>0?'partial':'complete',archive>0?`待归档 ${archive} 个`:'无需归档');
      target.innerHTML=`<div class="archive-stats"><div class="archive-stat"><span>总数</span><strong>${number(total,0)}</strong></div><div class="archive-stat"><span>保留</span><strong>${number(keep,0)}</strong></div><div class="archive-stat"><span>将归档</span><strong>${number(archive,0)}</strong></div><div class="archive-stat"><span>已归档</span><strong>${number(already,0)}</strong></div></div>${archivePlan?.plan_id?`<p class="form-note">计划 <span class="code-key">${escapeHtml(archivePlan.plan_id)}</span>；确认后只隐藏默认可选列表中的旧版本，不删除文件与报告。</p>`:'<p class="form-note">服务未返回归档计划标识，无法确认；请稍后重试或检查服务。</p>'}`;
      confirm.disabled=!archivePlan?.plan_id || archive===0;
      confirm.dataset.planId=archivePlan?.plan_id || '';
    }catch(error){
      if(error.name==='AbortError')return;
      archivePlan=null;
      confirm.disabled=true;
      if(error.status===404){
        chip.innerHTML=statusChip('neutral','归档接口未接入');
        target.innerHTML='<div class="notice"><strong>归档接口尚未就绪</strong><p>服务当前未提供模型归档；模型列表与默认方法不受影响。可稍后刷新重试。</p></div>';
        return;
      }
      chip.innerHTML=statusChip('blocked','预览失败');
      target.innerHTML=errorHtml(error);
    }
  }
  async function confirmArchive(button){
    const planId=archivePlan?.plan_id || button.dataset.planId;
    if(!planId){notify('请先预览归档范围，再确认归档。','error');return;}
    try{
      const result=await busy(button,()=>api.archiveConfirm(planId,signal),'正在归档…');
      if(signal.aborted)return;
      notify(`已归档 ${number(result?.archived,0)} 个模型版本。`);
      archivePlan=null;
      button.disabled=true;
      host.querySelector('[data-archive-chip]').innerHTML=statusChip('complete','已归档');
      host.querySelector('[data-archive-result]').innerHTML=`<div class="notice"><strong>归档完成</strong><p>${number(result?.archived,0)} 个版本已从默认可选列表隐藏；产物与历史报告保留。勾选“显示归档模型”可查看。</p></div>`;
      await Promise.all([loadModels(),loadDefaultMethod()]);
    }catch(error){if(error.name!=='AbortError')showError(host,error);}
  }
  host.querySelector('[data-archive-preview]').addEventListener('click',event=>previewArchive(event.currentTarget),{signal});
  host.querySelector('[data-archive-confirm]').addEventListener('click',event=>confirmArchive(event.currentTarget),{signal});
  host.querySelector('[data-refresh-method]').addEventListener('click',loadDefaultMethod,{signal});
  host.querySelector('[data-show-archived]').addEventListener('change',event=>{showArchived=event.target.checked;loadModels();},{signal});
  bindSymbolSearch(host.querySelector('#prediction-symbol'),signal);
  function setTab(value){host.querySelector('[data-training-section]').hidden=value!=='training';host.querySelector('[data-prediction-section]').hidden=value!=='prediction';host.querySelectorAll('[data-model-tab]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.modelTab===value)));}
  host.querySelectorAll('[data-model-tab]').forEach(button=>button.addEventListener('click',()=>setTab(button.dataset.modelTab),{signal}));setTab(params.get('tab')==='prediction'||params.get('symbol')?'prediction':'training');
  async function showPrediction(value){
    const result=value?.result || value;
    const target=host.querySelector('[data-prediction-result]');
    const reference=result.result_ref?.cache_key || result.prediction_ref || result.prediction_reference?.cache_key;
    let chartValue=result.chart || (result.schema==='prediction_chart_v1'?result:null);
    let chartError;
    if(!chartValue && reference){try{chartValue=await api.predictionChart(reference,signal);}catch(error){chartError=error;}}
    if(signal.aborted)return;
    predictionChart?.destroy();
    const quality=result.quality || chartValue?.quality || {},summary=result.summary || chartValue?.summary || {};
    target.innerHTML=`<div class="metrics" style="border:0;border-radius:0;margin:0"><div class="metric"><div class="metric-label">预测可用性</div><div class="metric-value" style="font-size:18px">${badge(quality.prediction_usable===true?'available':quality.prediction_usable===false?'unavailable':'unknown',quality.prediction_usable===undefined?'未返回':undefined)}</div></div><div class="metric"><div class="metric-label">中位数期末变化</div><div class="metric-value">${percent(summary.terminal_return_median)}</div><div class="metric-detail">相对截止日收盘，非可执行收益</div></div><div class="metric"><div class="metric-label">上涨采样比例</div><div class="metric-value">${percent(summary.up_path_fraction)}</div><div class="metric-detail">不是校准概率</div></div><div class="metric"><div class="metric-label">截止日期</div><div class="metric-value" style="font-size:20px">${escapeHtml(result.as_of || chartValue?.as_of || '—')}</div></div></div><div class="prediction-key"><span>已核验历史</span><span>原预测中位数 / 采样分位</span></div><div class="chart-area" data-prediction-chart></div><div class="chart-caption">未来日期来自核验交易日历。查看历史范围不重新推理，采样分位不构成置信区间。</div>${dataTable(result.forecast || chartValue?.forecast || [])}${disclosure('预测质量、参数与输入身份',{quality,input:result.input || chartValue?.input,parameters:result.parameters || chartValue?.parameters,identity:result.identity,history_verification:chartValue?.history_verification,error:chartError?.message})}`;
    const chartHost=target.querySelector('[data-prediction-chart]');
    try{if(chartValue)predictionChart=createPredictionChart(chartHost,chartValue);else chartHost.innerHTML=empty('已保存原预测，历史图暂不可读取',chartError?.message || '服务未返回可核验历史图引用。原预测数值和质量保持可读。');}catch(error){chartHost.innerHTML=errorHtml(error);}
  }

  host.querySelector('#prediction-form').addEventListener('submit',async event=>{
    event.preventDefault();const form=event.currentTarget;
    try{const symbol=symbolsFrom(formValue(form,'symbol'),1)[0],target=host.querySelector('[data-prediction-job]');target.hidden=false;predictionWatcher?.stop();predictionWatcher=await submitJob(target,form.querySelector('[type=submit]'),'prediction',{symbol,end:formValue(form,'end'),lookback:Number(formValue(form,'lookback')),horizon:Number(formValue(form,'horizon')),samples:Number(formValue(form,'samples')),seed:Number(formValue(form,'seed')),device:formValue(form,'device')},{signal,onComplete:async job=>{if(job.result)await showPrediction(job.result);if(job.error)showError(host,new Error(job.error));}});}catch(error){showError(host,error);}
  },{signal});
  async function openRun(runId){const target=host.querySelector('[data-learning-report]');target.innerHTML='<div class="panel-body">正在读取并核验报告…</div>';try{const report=await api.learningRun(runId,signal);if(!signal.aborted)target.innerHTML=learningReport(report);}catch(error){if(!signal.aborted)target.innerHTML=errorHtml(error);}}
  function renderRuns(){
    const target=host.querySelector('[data-learning-runs]');
    const total=learningRuns.length,shown=learningRunsExpanded?total:Math.min(5,total),rows=learningRuns.slice(0,shown);
    const toggleAria=learningRunsExpanded?'收起已保存实验列表，只显示最近 5 条':`查看全部 ${number(total,0)} 条已保存训练与滚动实验`;
    const toggleLabel=learningRunsExpanded?'收起，只看最近 5 条':`查看全部 ${number(total,0)} 条`;
    target.innerHTML=table([{label:'报告',render:row=>`<button class="small-button" data-learning-run="${escapeHtml(row.run_id)}">${escapeHtml(row.run_id)}</button>`},{label:'状态',render:row=>badge(row.status)},{label:'后端',render:row=>escapeHtml(row.spec?.backend || '—')},{label:'研究区间',render:row=>`${escapeHtml(row.spec?.start || '—')} — ${escapeHtml(row.spec?.end || '—')}`}],rows,{emptyTitle:'尚无训练报告',emptyDescription:'填写股票池与窗口后显式训练。'})+(total>5?`<div class="table-footer"><span class="small muted">显示 ${number(shown,0)} / ${number(total,0)} 条；默认只看最近 5 条。</span><button type="button" class="small-button" data-toggle-learning-runs aria-label="${escapeHtml(toggleAria)}" title="${escapeHtml(toggleAria)}">${escapeHtml(toggleLabel)}</button></div>`:'');
  }
  async function loadRuns(){try{const response=await api.learningRuns(signal);if(signal.aborted)return;const runs=arrayOf(response,'runs');learningRuns=runs.map((row,index)=>({row,index,key:String(row.created_at || '')})).sort((a,b)=>b.key.localeCompare(a.key)||a.index-b.index).map(item=>item.row);renderRuns();}catch(error){if(!signal.aborted)host.querySelector('[data-learning-runs]').innerHTML=errorHtml(error);}}
  async function loadModels(){try{const response=await api.models(signal,{includeArchived:showArchived});if(signal.aborted)return;const models=arrayOf(response,'models');const limit=showArchived?30:6,visible=models.slice(0,limit);host.querySelector('[data-model-list]').innerHTML=visible.length?visible.map(model=>`<article class="model-entry"><h3>${escapeHtml(modelName(model.model_name || model.model_type))} ${model.archived===true?statusChip('neutral','已归档'):''}</h3><p class="code-key">${escapeHtml(model.model_id)}</p>${modelSummary(model)}<div class="button-row space-top">${modelLinks(model)}<button class="small-button" data-model-detail-id="${escapeHtml(model.model_id)}">完整产物</button></div></article>`).join(''):empty(showArchived?'尚无冻结模型版本':'默认可选列表为空','训练完成后，在此明确选择用于扫描或回测的版本；也可勾选“显示归档模型”查看历史版本。');host.querySelector('[data-model-list]').insertAdjacentHTML('beforeend',visible.length?`<p class="model-list-more">显示 ${number(visible.length,0)} / 共 ${number(models.length,0)} 个版本${showArchived?'（含归档）':'（归档默认隐藏）'}；默认页面只需一个方法，其余版本保留用于研究与复核。</p>`:'');const caption=host.querySelector('[data-model-list-caption]');if(caption)caption.textContent=`${showArchived?'含归档版本':'默认不含归档'} · 已返回 ${number(models.length,0)} 个冻结版本`;const ready=response.assets?.ready;host.querySelector('[data-assets-state]').innerHTML=badge(ready===true?'available':ready===false?'missing':'unknown','Kronos '+(ready===true?'资源就绪':ready===false?'资源缺失':'资源状态未返回'));}catch(error){if(!signal.aborted)host.querySelector('[data-model-list]').innerHTML=errorHtml(error);}}
  const form=host.querySelector('#factor-training-form');
  form.querySelector('[name=scope]').addEventListener('change',event=>{const all=event.target.value==='all',symbols=form.querySelector('[name=symbols]');symbols.required=!all;symbols.disabled=all;},{signal});
  async function preflight(button){const spec=trainingSpec(form),result=await busy(button,()=>api.learningPreflight(spec,signal),'检查中…');if(signal.aborted)return result;host.querySelector('[data-learning-preflight-result]').innerHTML=`<div class="notice space-top">${badge(result.status || (result.ready===false?'blocked':'unknown'))}<p>${escapeHtml(result.reason || result.message || result.note || '请核对数据覆盖、设备和阻断项。')}</p><dl class="detail-list"><div><dt>人口 / 有行情股票</dt><dd>${number(result.population?.total,0)} / ${number(result.symbols_with_bars,0)}</dd></div><div><dt>已检查人口</dt><dd>${number(result.population_inspected,0)}</dd></div><div><dt>数据行 / 成熟标签行</dt><dd>${number(result.rows,0)} / ${number(result.mature_label_rows,0)}</dd></div><div><dt>特征矩阵估算字节</dt><dd>${number(result.estimated_feature_matrix_bytes,0)}</dd></div></dl>${result.blockers?.length?`<ul>${result.blockers.map(item=>`<li>${escapeHtml(typeof item==='string'?item:JSON.stringify(item))}</li>`).join('')}</ul>`:''}${disclosure('训练预检证据',result)}</div>`;return result;}
  host.querySelector('[data-learning-preflight]').addEventListener('click',async event=>{try{await preflight(event.currentTarget);}catch(error){showError(host,error);}},{signal});
  form.addEventListener('submit',async event=>{event.preventDefault();try{const spec=trainingSpec(form),target=host.querySelector('[data-factor-training-job]');target.hidden=false;trainingWatcher?.stop();host.querySelector('[data-learning-report]').replaceChildren();trainingWatcher=await submitJob(target,form.querySelector('[type=submit]'),'factor_training',spec,{signal,onComplete:async job=>{if(signal.aborted)return;if(job.result?.run_id)await openRun(job.result.run_id);if(job.error)showError(host,new Error(job.error));await Promise.all([loadModels(),loadRuns()]);}});}catch(error){showError(host,error);}},{signal});
  host.querySelector('[data-refresh-models]').addEventListener('click',loadModels,{signal});host.querySelector('[data-refresh-learning]').addEventListener('click',loadRuns,{signal});
  host.querySelector('[data-learning-report]').addEventListener('click',async event=>{const button=event.target.closest('[data-refit-run]');if(!button)return;try{const target=host.querySelector('[data-refit-job]');target.hidden=false;refitWatcher?.stop();refitWatcher=await submitJob(target,button,'model_refit',{run_id:button.dataset.refitRun,model_name:button.dataset.refitName},{signal,onComplete:async job=>{if(signal.aborted)return;if(job.result?.run_id)await openRun(job.result.run_id);if(job.error)showError(host,new Error(job.error));await Promise.all([loadModels(),loadRuns()]);}});}catch(error){showError(host,error);}},{signal});
  host.querySelector('[data-learning-runs]').addEventListener('click',event=>{const toggle=event.target.closest('[data-toggle-learning-runs]');if(toggle){learningRunsExpanded=!learningRunsExpanded;renderRuns();return;}const button=event.target.closest('[data-learning-run]');if(button)openRun(button.dataset.learningRun);},{signal});
  host.querySelector('[data-model-list]').addEventListener('click',async event=>{const button=event.target.closest('[data-model-detail-id]');if(!button)return;try{const detail=await busy(button,()=>api.model(button.dataset.modelDetailId,signal),'核验中…');if(!signal.aborted)host.querySelector('[data-model-detail]').innerHTML=disclosure('冻结产物与模型详情',detail,true);}catch(error){showError(host,error);}},{signal});
  async function loadCatalog(){try{const catalog=await api.learningCatalog(signal);if(signal.aborted)return;host.querySelector('[data-learning-catalog]').innerHTML=table([{key:'id',label:'后端'},{label:'状态',render:row=>badge(row.available?'available':'unavailable')},{key:'reason',label:'运行条件',wrap:true}],arrayOf(catalog,'backends'))+disclosure('因子目录、模型能力与默认参数',catalog);for(const backend of arrayOf(catalog,'backends')){const option=form.querySelector(`select[name=backend] option[value="${backend.id==='qlib'?'qlib':'local'}"]`);if(option)option.disabled=!backend.available;}}catch(error){if(!signal.aborted)host.querySelector('[data-learning-catalog]').innerHTML=errorHtml(error);}}
  await Promise.all([loadModels(),loadRuns(),loadCatalog(),loadDefaultMethod(),loadForward(),mountStudy(host.querySelector('[data-study-center]'),{signal}).then(cleanup=>{studyCleanup=cleanup;})]);if(params.get('run_id'))await openRun(params.get('run_id'));
  return()=>{trainingWatcher?.stop();predictionWatcher?.stop();refitWatcher?.stop();predictionChart?.destroy();studyCleanup?.();};
}

const learningModelLabel=name=>({legacy_ma_ridge:'均线特征 · Ridge 基线',price_volume_ridge:'纯量价 38 项 · Ridge',price_volume_lightgbm:'纯量价 38 项 · LightGBM',factor_ridge:'多因子 · Ridge',factor_lightgbm:'多因子 · LightGBM',factor_mlp:'多因子 · 均线 MLP',legacy_ma_mlp:'均线特征 · MLP 基线',price_volume_mlp:'纯量价 · 均线 MLP'})[name] || name;
const percentagePoints=value=>Number.isFinite(value)?`${number(value*100,2)} 个百分点`:'—';

// Pure renderer for the forward ledger; covered by node tests without a DOM.
export function forwardSummaryBody(value){
  const days=value?.days || {},sample=value?.sample || {},latest=value?.latest || null,rolling=value?.rolling || {},kronos=value?.kronos || {},gates=arrayOf(value,'gates');
  if(!Number(days.total||0))return empty('尚无前瞻记录','收盘后运行 forward record 冻结信号，下一交易日数据发布后运行 forward verify 对账；没有记录时页面不显示任何成绩。');
  const coverage=kronos.coverage;
  const gateLabel={data:'数据核验',agreement:'互证闸门',realized:'实际对账'};
  const statusText={pass:'通过',fail:'未通过',pending:'待定',complete:'完整',observed:'已观测',partial:'部分'};
  const latestKpis=latest?`<dl class="detail-list"><div><dt>最近对账</dt><dd>${escapeHtml(latest.day)} · 已验证 ${number(latest.n,0)} 只 / 请求 ${number(latest.requested,0)}；覆盖 ${percent(latest.coverage)}</dd></div><div><dt>Rank IC / 命中率</dt><dd>${number(latest.rank_ic,4)} / ${percent(latest.hit_rate)}</dd></div><div><dt>互证一致性</dt><dd>sign_agree ${percent(latest.sign_agree)} · Top-K 重合 ${percent(latest.topk_overlap)} · 排序相关 ${number(latest.rank_agree,3)}</dd></div><div><dt>2× 成本后均值</dt><dd>${percent(latest.mean_ret_net_2x)}（毛收益均值 ${percent(latest.mean_ret)}）</dd></div>${Number(latest.revision||0)?`<div><dt>数据修订</dt><dd>revision ${number(latest.revision,0)}（行情修订后新增对账行，旧行保留）</dd></div>`:''}</dl>`:'<p class="form-note">尚无已核验交易日；记录后需等下一交易日数据发布才能对账，不会用估算值代替。</p>';
  const rollingRows=[['近 5 日',rolling.last5],['近 20 日',rolling.last20],['近 60 日',rolling.last60]].filter(item=>item[1]).map(([label,row])=>({label,days:row.days,ic:row.rank_ic_mean,agree:row.sign_agree_mean,coverage:row.coverage_mean}));
  const bucketRows=Object.entries(rolling.buckets||{}).map(([name,row])=>({name:({A:'A 双方看多',B:'B 因子多 / Kronos 空',C:'C 因子空 / Kronos 多',D:'D 双方看空'})[name]||name,count:row.count,mean:row.mean_ret,hit:row.hit_rate}));
  return `<div class="notice">${sample.status==='sufficient'?`前瞻证据已累计 ${number(sample.verified_days,0)} 个独立信号日。`:`前瞻证据累计 ${number(sample.verified_days,0)} / ${number(sample.required_days,0)} 个独立信号日：只描述现象，不允许据此调整协议或阈值。`} Kronos 可用 ${number(kronos.succeeded,0)} / ${number(kronos.requested,0)}${coverage===null||coverage===undefined?'':'（'+percent(coverage)+'）'}；不可用的标的按因子兜底并保留原因，不用替代值冒充。</div>
  <dl class="detail-list"><div><dt>账本</dt><dd>共 ${number(days.total,0)} 日 · 已核验 ${number(days.verified,0)} · 部分 ${number(days.partial,0)} · 待对账 ${number(days.recorded,0)} · 实时 ${number(days.live,0)} / 回填 ${number(days.backfill,0)}</dd></div></dl>
  ${latestKpis}
  ${rollingRows.length?`<h3 class="space-top">滚动证据</h3>${table([{key:'label',label:'窗口'},{label:'独立信号日',render:row=>number(row.days,0)},{label:'Rank IC 均值',render:row=>number(row.ic,4)},{label:'sign_agree 均值',render:row=>percent(row.agree)},{label:'覆盖均值',render:row=>percent(row.coverage)}],rollingRows)}`:''}
  ${bucketRows.some(row=>row.count)?`<h3 class="space-top">互证分桶（实际收益）</h3>${table([{key:'name',label:'分桶'},{label:'样本',render:row=>number(row.count,0)},{label:'均值收益',render:row=>percent(row.mean)},{label:'命中率',render:row=>percent(row.hit)}],bucketRows)}`:''}
  ${gates.length?`<h3 class="space-top">最近一日闸门</h3>${table([{label:'闸门',render:row=>escapeHtml(gateLabel[row.gate]||row.gate)},{label:'状态',render:row=>badge(row.status,statusText[row.status]||row.status)},{label:'说明',render:row=>escapeHtml(row.detail?.reason||'—'),wrap:true}],gates)}`:''}
  ${arrayOf(value,'limitations').length?`<p class="form-note">${arrayOf(value,'limitations').map(item=>escapeHtml(item)).join('；')}</p>`:''}`;
}

export function learningReport(report) {
  const models=arrayOf(report,'models');
  const dataset=report.dataset || {};
  const selectedGroups=new Set([...(report.spec?.factor_groups || Object.keys(report.feature_groups || {})),'legacy_ma']);
  const sources=Object.entries(report.sources?.datasets || {}).map(([name,source])=>({name,...source}));
  return `<div class="panel-header"><div><h3>${escapeHtml(report.run_id || '多因子研究报告')}</h3><p>${escapeHtml(report.spec?.start || '—')} 至 ${escapeHtml(report.spec?.end || '—')} · ${escapeHtml(report.spec?.backend || '—')} · ${report.spec?.scope==='all'?'全市场冻结人口':number(report.spec?.symbols?.length,0)+' 只显式股票'}</p></div>${badge(report.status)}</div><div class="panel-body"><p class="risk-note">研究用途 · 严格历史资格 ${report.qualification?.strict_history_eligible===true?'通过':'未通过'} · 模拟配置资格 ${report.qualification?.paper_allocation_eligible===true?'通过':'未通过'}。收益须结合执行局限与数据可得时间解释。</p>${report.benchmark?`<h3 class="space-top">同样本等权基准</h3><dl class="detail-list"><div><dt>研究账户收益（费用后）</dt><dd>${percent(report.benchmark.metrics?.total_return)}</dd></div><div><dt>成交 / 拒单</dt><dd>${number(report.benchmark.metrics?.fills,0)} / ${number(report.benchmark.metrics?.rejected_orders,0)}</dd></div><div><dt>费用（元）</dt><dd>${number(report.benchmark.metrics?.fees)}</dd></div></dl><p class="small muted">与模型使用同一可推理样本、交易日历和 Broker；表中超额为总收益相减的百分点差，非指数超额。</p>`:''}${table([
    {label:'模型 / 输入',render:row=>{const counts=(row.fold_models || []).map(fold=>fold.feature_count).filter(Number.isFinite);return `${escapeHtml(learningModelLabel(row.name))}<br><span class="small muted">配置 ${number(row.feature_count,0)} 项 · 逐窗口实际 ${counts.length?`${number(Math.min(...counts),0)}–${number(Math.max(...counts),0)}`:'未返回'}</span>`;}},
    {label:'样本外 Rank IC',render:row=>number(row.metrics?.rank_ic,4)},
    {label:'样本外 IC',render:row=>number(row.metrics?.ic,4)},
    {label:'RMSE',render:row=>number(row.metrics?.rmse,4)},
    {label:'观察 / 日期',render:row=>`${number(row.metrics?.observations,0)} / ${number(row.metrics?.dates,0)}`},
    {label:'研究账户收益（费用后）',render:row=>percent(row.portfolio?.metrics?.total_return)},
    {label:'相对等权基准超额',render:row=>percentagePoints(row.excess_return_vs_equal_weight)},
    {label:'最大回撤',render:row=>percent(row.portfolio?.metrics?.max_drawdown)},
    {label:'最终模型',render:row=>report.family==='cross_sectional_return_v1'?`<button class="small-button" data-refit-run="${escapeHtml(report.run_id)}" data-refit-name="${escapeHtml(row.name)}">按此协议最终重拟合</button>`:report.family==='frozen_factor_refit_v1'?'最终重拟合产物；无独立样本外成绩':'—'}
  ],models,{emptyTitle:'没有模型结果',emptyDescription:report.reason || '展开报告查看阻断原因。'})}<h3 class="space-top">研究账户执行情况</h3>${table([
    {label:'模型',render:row=>escapeHtml(learningModelLabel(row.name))},
    {label:'成交 / 拒单 / 部分成交',render:row=>`${number(row.portfolio?.metrics?.fills,0)} / ${number(row.portfolio?.metrics?.rejected_orders,0)} / ${number(row.portfolio?.metrics?.partial_orders,0)}`},
    {label:'费用（元）',render:row=>number(row.portfolio?.metrics?.fees)},
    {label:'沿用旧价估值日',render:row=>number(row.portfolio?.metrics?.stale_valuation_days,0)},
    {label:'Sharpe',render:row=>number(row.portfolio?.metrics?.sharpe,3)},
    {label:'执行局限',render:row=>disclosure('查看限制',row.portfolio?.limitations || []),wrap:true}
  ],models)}<h3 class="space-top">输入覆盖与标签</h3><dl class="detail-list"><div><dt>实际行情股票 / 请求股票</dt><dd>${number(dataset.symbols_with_bars,0)} / ${number(dataset.symbols_requested?.length,0)}</dd></div><div><dt>数据行 / 有效行情行 / 特征合格行</dt><dd>${number(dataset.rows,0)} / ${number(dataset.valid_bar_rows,0)} / ${number(dataset.feature_eligible_rows,0)}</dd></div><div><dt>标签公式</dt><dd>${escapeHtml(dataset.label_formula || '未返回')}</dd></div><div><dt>复权与总体历史</dt><dd>${escapeHtml(dataset.adjustment_status || '未返回')} · 历史股票池${dataset.historical_population_complete===true?'完整':'未核验完整'}</dd></div></dl>${table([
    {key:'group',label:'因子组'},{label:'有数据行 / 完整行',render:row=>`${number(row.nonempty_rows,0)} / ${number(row.complete_rows,0)}`},{label:'行覆盖',render:row=>percent(row.row_coverage)},{key:'reason',label:'覆盖说明',wrap:true}
  ],Object.entries(dataset.group_coverage || {}).map(([group,coverage])=>({group,...coverage})))}<h3 class="space-top">滚动样本外窗口</h3>${table([
    {key:'fold',label:'窗口'},
    {label:'训练期',render:row=>`${escapeHtml(row.train_start)} — ${escapeHtml(row.train_end)}`},
    {label:'验证期',render:row=>`${escapeHtml(row.valid_start)} — ${escapeHtml(row.valid_end)}`},
    {label:'测试期',render:row=>`${escapeHtml(row.test_start)} — ${escapeHtml(row.test_end)}`},
    {label:'训练 / 验证 / 测试行',render:row=>`${number(row.train_rows,0)} / ${number(row.valid_rows,0)} / ${number(row.test_rows,0)}`}
  ],arrayOf(report,'folds'))}<h3 class="space-top">固定数据来源</h3><p class="small muted">读取身份 ${escapeHtml(report.sources?.read_ref || '未返回')}</p>${table([{key:'name',label:'数据集'},{key:'revision_id',label:'代际标识',wrap:true},{label:'文件数',render:row=>number(row.files?.length,0)}],sources)}<h3 class="space-top">配置输入特征</h3><p class="small muted">逐窗口训练期全缺失列会被剔除，实际维度及剔除列以模型产物为准。</p>${table([{key:'group',label:'因子组'},{key:'count',label:'特征数'},{key:'features',label:'特征名称',wrap:true}],Object.entries(report.feature_groups || {}).filter(([group])=>selectedGroups.has(group)).map(([group,features])=>({group,count:features.length,features:features.join('、')})))}${arrayOf(report,'limitations').length?`<div class="notice space-top"><strong>本次研究边界</strong><ul>${report.limitations.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul></div>`:''}${disclosure('数据覆盖与可得时间',dataset)}${disclosure('固定数据来源与代际身份',report.sources)}${report.benchmark?disclosure('等权基准执行与局限',report.benchmark):''}${disclosure('逐模型组合指标与执行局限',models.map(({name,portfolio})=>({name,portfolio})))}${fullReportLink(report)}${disclosure('界面摘要与产物校验',report)}</div>`;
}

