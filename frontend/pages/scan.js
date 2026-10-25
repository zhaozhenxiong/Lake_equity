import {api} from '../core/api.js';
import {arrayOf,badge,busy,disclosure,empty,errorHtml,escapeHtml,formValue,heading,number,routeLink,showError,symbolsFrom,table,today} from '../core/ui.js';
import {submitJob} from '../core/jobs.js';
import {reportList} from '../core/reports.js';
import {bindModelSelector} from '../core/models.js';
import {releaseSummary} from '../core/study.js';

export function scanPage(rows,{query='',status='',page=1,pageSize=100}={}){
  const term=query.toLowerCase(),filtered=rows.filter(row=>(!status||row.status===status)&&(!term||`${row.symbol} ${row.name || ''} ${row.reason || ''}`.toLowerCase().includes(term)));
  const pages=Math.max(1,Math.ceil(filtered.length/pageSize)),current=Math.min(Math.max(page,1),pages);
  return {rows:filtered.slice((current-1)*pageSize,current*pageSize),total:filtered.length,received:rows.length,pages,page:current};
}

// Concrete release_id (never rewritten to active) plus the matching universe scope for explicit scans.
export function scanSpec(form,{activeRelease}={}){
  const scope=formValue(form,'scope'),fixed=formValue(form,'version_source')==='frozen',mode=formValue(form,'scan_mode');
  const spec={scope,symbols:scope==='all'?[]:symbolsFrom(formValue(form,'symbols'),6000),end:formValue(form,'end')};
  const universeScope=formValue(form,'universe_scope');
  if(scope==='symbols'&&universeScope)spec.universe_scope=universeScope;
  if(fixed){spec.model_id=formValue(form,'model_id');if(!spec.model_id)throw new Error('请选择冻结模型版本。');if(mode!=='scan')throw new Error('Kronos融合绑定阶段发布版本，请选择阶段默认研究版本。');}
  else{if(!activeRelease)throw new Error('请先在模型中心启用默认方法。');spec.release_id=formValue(form,'release_id')||'active';}
  return {spec,mode};
}

export async function mount(host,{signal,params}){
  let watcher,result,page=1;
  host.innerHTML=heading('全市场模型选股','使用明确冻结的模型版本，按实际沪深 A 股人口分批推理；分开记录评分、缺失、失败与未处理。')+`<section class="panel"><div class="panel-header"><h2>选择模型与扫描范围</h2></div><form id="scan-form" class="form-toolbar"><div class="form-grid"><label class="span-two">冻结模型版本<select name="model_id" data-model-select required><option value="">正在读取模型版本…</option></select></label><label>研究范围<select name="scope"><option value="symbols">指定股票池</option><option value="all">全部沪深 A 股</option></select></label><label class="span-two">股票代码<input name="symbols" value="${escapeHtml(params.get('symbols') || '')}" placeholder="600519.SH,000001.SZ" required></label><label>行情截止<input name="end" type="date" value="${escapeHtml(params.get('end') || today())}" required></label><button type="submit" class="primary">使用该版本扫描</button></div><p class="form-note">全市场范围由后端冻结人口并分批处理，不以训练股票池代替全市场。分数不是上涨概率或直接买卖指令。</p><div data-model-identity></div></form><div class="job-inline" data-scan-job hidden></div></section><section class="panel space-top"><div class="panel-header"><div><h2>扫描结果</h2><p data-scan-caption>等待显式扫描或重开保存报告</p></div><div class="button-row"><input data-scan-filter aria-label="筛选扫描结果" placeholder="代码、名称、原因"><select data-scan-status aria-label="评分状态"><option value="">全部状态</option><option value="scored">已评分</option><option value="missing">输入缺失</option><option value="failed">推理失败</option><option value="unprocessed">尚未处理</option></select></div></div><div class="panel-body" data-scan-population></div><div data-scan-results>${empty('尚无模型扫描结果','先在模型中心训练，再明确选择一个冻结版本。')}</div><div class="table-footer"><button class="small-button" data-scan-previous disabled>上一页</button><span data-scan-pagination></span><button class="small-button" data-scan-next disabled>下一页</button></div><div data-scan-identity></div></section><div class="section-divider"><h2>保存的模型扫描</h2><button class="small-button" data-refresh-saved>刷新索引</button></div><section class="panel" data-saved-scans></section>`;
  const form=host.querySelector('#scan-form');
  form.querySelector('.form-grid').insertAdjacentHTML('afterbegin',`<label>策略版本<select name="version_source"><option value="active">阶段默认研究版本</option><option value="frozen">明确选择冻结模型</option></select></label><label>选股流程<select name="scan_mode"><option value="scan">多因子与均线排序</option><option value="factor_baseline">多因子 + Kronos对照（λ=0）</option><option value="kronos_filter">Kronos条件筛选（研究规则）</option></select></label>`);
  form.querySelector('.form-grid').insertAdjacentHTML('beforeend',`<label data-fusion-field hidden>持仓账户（可选）<select name="account_id"><option value="">无持仓账户</option></select></label><label data-fusion-field hidden>初筛候选数<input name="candidate_count" type="number" value="200" min="1" max="200"></label><label data-fusion-field hidden>本次预测调用预算<input name="max_calls" type="number" value="20" min="1" max="200"></label><label data-fusion-field hidden>本次预算秒数<input name="budget_seconds" type="number" value="300" min="1" max="1800"></label><label data-fusion-field hidden>预测设备<select name="kronos_device"><option value="cuda">CUDA</option><option value="cpu">CPU</option></select></label>`);
  form.insertAdjacentHTML('beforeend','<div data-scan-release></div>');
  form.insertAdjacentHTML('beforeend',`<input type="hidden" name="release_id" value="${escapeHtml(params.get('release_id') || '')}"><input type="hidden" name="universe_scope" value="${escapeHtml(params.get('universe_scope') || '')}">`);
  // Default view: target date + run the published default method. Version/mode/model/scope live in the advanced disclosure.
  const grid=form.querySelector('.form-grid');
  const endField=grid.querySelector('[name=end]').closest('label');
  const submitButton=form.querySelector('button[type=submit]');
  const simple=document.createElement('div');
  simple.className='run-simple';
  const methodBox=document.createElement('div');
  methodBox.className='run-default';
  methodBox.innerHTML='<span>默认方法</span><strong data-scan-default-name>读取中…</strong><code data-scan-default-id>—</code>';
  submitButton.textContent='运行默认方法选股';
  submitButton.setAttribute('aria-label','使用默认方法运行全市场选股');
  submitButton.setAttribute('title','使用已发布的默认方法，按全市场冻结人口选股；版本与范围可在高级区调整');
  submitButton.classList.add('run-submit');
  simple.append(methodBox,endField,submitButton);
  grid.before(simple);
  const advanced=document.createElement('details');
  advanced.className='advanced-panel';
  advanced.innerHTML='<summary>高级：版本、流程与范围</summary><div class="advanced-body"></div>';
  const advancedBody=advanced.querySelector('.advanced-body');
  grid.before(advanced);
  advancedBody.append(grid,form.querySelector('[data-model-identity]'),form.querySelector('[data-scan-release]'),form.querySelector('.form-note'));
  let activeRelease=null;
  function sourceChanged(){const fixed=formValue(form,'version_source')==='frozen',fusion=formValue(form,'scan_mode')!=='scan';form.querySelector('[name=model_id]').closest('label').hidden=!fixed;form.querySelector('[name=model_id]').required=fixed;host.querySelector('[data-model-identity]').hidden=!fixed;host.querySelector('[data-scan-release]').hidden=fixed;host.querySelectorAll('[data-fusion-field]').forEach(el=>{el.hidden=!fusion;});}
  form.querySelector('[name=version_source]').value=params.get('model_id')?'frozen':'active';
  form.querySelector('[name=scope]').value=params.get('symbols')?'symbols':'all';if(['factor_baseline','kronos_filter'].includes(params.get('scan_mode')))form.querySelector('[name=scan_mode]').value=params.get('scan_mode');host.querySelector('.page-heading p').textContent='默认只运行一个默认方法选股：目标日 + 全市场冻结人口；版本、流程与范围可在高级区调整。';form.querySelector('.form-note').textContent='默认提交当前默认方法的 release_id（关注页带入时使用具体发布版本），显式股票池会带 universe_scope=explicit_symbols；本次扫描不会重训模型。分数不是上涨概率，延期、失败与池外股票保留在分母中。';
  function scopeChanged(){const all=formValue(form,'scope')==='all';form.querySelector('[name=symbols]').required=!all;form.querySelector('[name=symbols]').disabled=all;}
  sourceChanged();scopeChanged();form.querySelector('[name=version_source]').addEventListener('change',sourceChanged,{signal});form.querySelector('[name=scan_mode]').addEventListener('change',sourceChanged,{signal});
  function render(){
    const view=scanPage(arrayOf(result,'rows'),{query:host.querySelector('[data-scan-filter]').value,status:host.querySelector('[data-scan-status]').value,page});page=view.page;
    const releaseId=activeRelease?.release_id || params.get('release_id') || 'active';
    host.querySelector('[data-scan-results]').innerHTML=table([
      {key:'rank',label:'排名'},
      {label:'股票',render:row=>`<a class="symbol" href="${escapeHtml(routeLink('analysis',{symbol:row.symbol,end:result?.as_of || result?.end}))}">${escapeHtml(row.symbol)}</a><div class="small muted">${escapeHtml(row.name || '')}</div>`},
      {label:'模型分数',render:row=>number(row.score ?? row.factor_score,5)},
      {label:'Kronos状态',render:row=>escapeHtml(row.kronos_status || '—')},
      {label:'状态',render:row=>badge(row.status,({scored:'已评分',missing:'输入缺失',failed:'推理失败',unprocessed:'未处理',excluded:'动态池排除'})[row.status])},
      {label:'冻结研究人口',render:row=>row.is_training_member===true?'研究人口内':row.is_training_member===false?'研究人口外':'未返回'},
      {key:'reason',label:'原因',wrap:true},
      {label:'操作',render:row=>`<a href="${escapeHtml(routeLink('backtest',{release_id:releaseId,mode:'frozen',symbols:row.symbol,end:result?.as_of || result?.end,universe_scope:'explicit_symbols'}))}">用默认方法回测该股</a>`}
    ],view.rows,{emptyTitle:'没有匹配的评分行',emptyDescription:'检查状态筛选与覆盖，缺失不等同于低分。'});
    host.querySelector('[data-scan-pagination]').textContent=`第 ${page} / ${view.pages} 页 · 筛选 ${view.total} / 完整返回 ${view.received} 行`;
    host.querySelector('[data-scan-previous]').disabled=page<=1;host.querySelector('[data-scan-next]').disabled=page>=view.pages;
  }
  function showResult(value){result=value?.result || value;page=1;const fusion=result.family==='fusion_scan_v1';if(fusion)result={...result,rows:(result.rows || []).map(row=>({...row,score:row.factor_score,rank:row.fused_rank,status:row.baseline_status}))};const population=result.population || {};host.querySelector('[data-scan-caption]').textContent=`${result.as_of || result.end || '—'} · 模型 ${result.model_id || '未返回'} · 报告 ${result.run_id || '—'}`;host.querySelector('[data-scan-population]').innerHTML=fusion?`<dl class="detail-list">${[['baseline_total','全市场初筛分母'],['total','复核总数（候选与持仓去重）'],['candidates','候选'],['holdings','持仓'],['succeeded','预测可用'],['unusable','预测不可用'],['failed','失败'],['deferred','预算延期']].map(([key,label])=>`<div><dt>${label}</dt><dd>${number(result.coverage?.[key],0)}</dd></div>`).join('')}<div><dt>规则后可选</dt><dd>${number(arrayOf(result,'rows').filter(row=>row.is_candidate===true&&row.selection_eligible===true).length,0)}</dd></div></dl><p>${result.final_ranking?'本次排名已冻结':'覆盖未完成，不能视为最终排名'}；下一交易日 ${escapeHtml(result.next_session)}。${result.mode==='factor_baseline'?'Kronos选择权重为0，仅作对照。':'Kronos条件筛选为未经独立验证的研究规则。'}</p><div class="button-row"><a class="button" href="${routeLink('watchlist',{tab:'trading',fusion_id:result.run_id,account_id:formValue(form,'account_id')})}">结合持仓构建次日计划</a>${result.coverage?.deferred?`<button class="small-button" data-resume-fusion="${escapeHtml(result.run_id)}">按原版本继续未完成预测</button>`:''}</div>`:`<dl class="detail-list">${[['total','冻结总人口'],['scored','已评分'],['excluded','动态池排除'],['missing','缺失'],['failed','失败'],['unprocessed','未处理']].map(([key,label])=>`<div><dt>${label}</dt><dd>${number(population[key],0)}</dd></div>`).join('')}</dl>${badge(result.status)}<p class="small muted">完整人口与动态池排除均保留；分页和筛选只改变展示。</p>`;host.querySelector('[data-scan-identity]').innerHTML=disclosure('版本、动态股票池、覆盖与预测完整证据',result);render();}
  async function openReport(id){try{const report=await api.report(id,signal);if(!signal.aborted)showResult(report);}catch(error){showError(host,error);}}
  host.querySelector('[data-scan-filter]').addEventListener('input',()=>{page=1;render();},{signal});host.querySelector('[data-scan-status]').addEventListener('change',()=>{page=1;render();},{signal});
  host.querySelector('[data-scan-previous]').addEventListener('click',()=>{page--;render();},{signal});host.querySelector('[data-scan-next]').addEventListener('click',()=>{page++;render();},{signal});
  form.querySelector('[name=scope]').addEventListener('change',event=>{const all=event.target.value==='all';form.querySelector('[name=symbols]').required=!all;form.querySelector('[name=symbols]').disabled=all;},{signal});
  async function startScan(kind,spec,button){const target=host.querySelector('[data-scan-job]');target.hidden=false;watcher?.stop();watcher=await submitJob(target,button,kind,spec,{signal,onComplete:async job=>{if(job.result)showResult(job.result);else if(job.run_id)await openReport(job.run_id);if(job.error)showError(host,new Error(job.error));await loadReports();}});}
  form.addEventListener('submit',async event=>{event.preventDefault();try{const {spec,mode}=scanSpec(form,{activeRelease});if(mode!=='scan'){Object.assign(spec,{mode,candidate_count:Number(formValue(form,'candidate_count')),max_calls:Number(formValue(form,'max_calls')),budget_seconds:Number(formValue(form,'budget_seconds')),kronos:{device:formValue(form,'kronos_device')}});const accountId=formValue(form,'account_id');if(accountId){const account=await api.tradingAccount(accountId,signal);if(account.snapshot.as_of!==spec.end)throw new Error('持仓快照日期须与行情截止一致，请先更新账户快照。');spec.holding_symbols=account.snapshot.positions.map(row=>row.symbol);}}await startScan(mode==='scan'?'scan':'fusion_scan',spec,form.querySelector('[type=submit]'));}catch(error){showError(host,error);}},{signal});
  host.querySelector('[data-scan-population]').addEventListener('click',async event=>{const button=event.target.closest('[data-resume-fusion]');if(!button)return;try{await startScan('fusion_scan',{resume_id:button.dataset.resumeFusion,max_calls:Number(formValue(form,'max_calls')),budget_seconds:Number(formValue(form,'budget_seconds'))},button);}catch(error){showError(host,error);}},{signal});
  async function loadReports(){try{const response=await api.reports(signal);if(signal.aborted)return;host.querySelector('[data-saved-scans]').innerHTML=reportList(arrayOf(response,'reports').filter(row=>['model_scan_v1','fusion_scan_v1'].includes(row.family)||['model_scan','scan','fusion'].includes(row.kind)),{limit:30});}catch(error){if(!signal.aborted)host.querySelector('[data-saved-scans]').innerHTML=errorHtml(error);}}
  host.querySelector('[data-refresh-saved]').addEventListener('click',loadReports,{signal});host.querySelector('[data-saved-scans]').addEventListener('click',event=>{const button=event.target.closest('[data-report-id]');if(button)openReport(button.dataset.reportId);},{signal});
  host.querySelector('[data-scan-status]').insertAdjacentHTML('beforeend','<option value="excluded">动态池排除</option>');
  await Promise.all([loadReports(),bindModelSelector(host,{signal,selected:params.get('model_id') || ''}).catch(error=>showError(host,error)),api.activeRelease(signal).then(async value=>{if(signal.aborted)return;activeRelease=value.release;const requested=params.get('release_id');if(requested&&requested!=='active'&&activeRelease?.release_id!==requested){try{const all=await api.releases(signal);if(signal.aborted)return;activeRelease=arrayOf(all,'releases').find(row=>row.release_id===requested)||activeRelease;}catch(error){if(error.name!=='AbortError')throw error;}}host.querySelector('[data-scan-release]').innerHTML=releaseSummary(activeRelease);const nameHost=host.querySelector('[data-scan-default-name]'),idHost=host.querySelector('[data-scan-default-id]');if(nameHost)nameHost.textContent=activeRelease?.name || '尚无默认方法';if(idHost)idHost.textContent=activeRelease?`${activeRelease.release_id} · 信息截止 ${activeRelease.information_cutoff || '—'}`:'请先到模型中心启用默认方法';}).catch(error=>showError(host,error)),api.tradingAccounts(signal).then(value=>{if(signal.aborted)return;form.querySelector('[name=account_id]').insertAdjacentHTML('beforeend',arrayOf(value,'accounts').filter(row=>!row.is_fixture).map(row=>`<option value="${escapeHtml(row.account_id)}">${escapeHtml(row.name)}</option>`).join(''));const requested=params.get('account_id');if(requested&&[...form.elements.account_id.options].some(o=>o.value===requested))form.elements.account_id.value=requested;}).catch(error=>showError(host,error))]);if(params.get('report'))await openReport(params.get('report'));
  return()=>watcher?.stop();
}
