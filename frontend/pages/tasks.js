import {api} from '../core/api.js';
import {activeJob,arrayOf,badge,busy,dateTime,disclosure,empty,errorHtml,escapeHtml,heading,jobId,kindLabel,notify,number,progressValue,showError,statusLabel,table} from '../core/ui.js';
import {bindReportLinks,isPipelinePreview,openReport,renderReport,reportList,reportStatusLabel} from '../core/reports.js';
import {cancellationNotice,progressSummary} from '../core/jobs.js';

// Terminal failures can carry per-group / per-dataset reasons; surface them before the raw log.
export function resultErrorItems(result){
  const items=[];
  const push=(value,fallbackGroup)=>{
    if(!value)return;
    if(typeof value==='string'){items.push({group:fallbackGroup || '运行',dataset:'—',reason:value,code:''});return;}
    if(Array.isArray(value)){value.forEach(item=>push(item,fallbackGroup));return;}
    if(typeof value!=='object')return;
    const reason=value.reason || value.message || value.error || value.detail || value.note || value.reason_code || value.code;
    if(reason || value.group || value.dataset || value.name){items.push({group:value.group || value.stage || value.phase || fallbackGroup || '运行',dataset:value.dataset || value.name || value.symbol || '—',reason:reason || '—',code:value.reason_code || value.code || ''});}
    else items.push({group:fallbackGroup || '运行',dataset:'—',reason:JSON.stringify(value),code:''});
  };
  push(result?.blockers,'阻断');
  push(result?.errors,'错误');
  push(result?.failures,'失败');
  push(result?.groups,'组');
  push(result?.datasets,'数据集');
  push(result?.error,'运行');
  push(result?.coverage?.blockers,'覆盖阻断');
  const seen=new Set();
  return items.filter(item=>{const key=JSON.stringify(item);if(seen.has(key))return false;seen.add(key);return true;});
}

export function resultErrorSummary(result){
  if(!result || !['failed','blocked'].includes(result.status))return '';
  const items=resultErrorItems(result);
  return `<div class="notice error space-top" role="alert"><strong>${escapeHtml(statusLabel(result.status))} · 结构化错误摘要</strong>${items.length?table([{label:'组',render:row=>escapeHtml(row.group)},{label:'数据集 / 对象',render:row=>escapeHtml(row.dataset)},{label:'原因',render:row=>`${escapeHtml(row.reason)}${row.code?` <span class="small muted">${escapeHtml(row.code)}</span>`:''}`,wrap:true}],items):'<p>产物状态为失败或已阻止，但未返回逐项原因；请展开下方完整结果与日志核对。</p>'}</div>`;
}

// Confirmation body for failed-build cleanup: unique non-empty ids plus the current preview plan_id.
export function quarantineConfirmBody(planId,ids){
  return {plan_id:String(planId || ''),ids:[...new Set((Array.isArray(ids)?ids:[]).map(id=>String(id || '').trim()).filter(Boolean))],confirm:true};
}

export async function mount(host,{signal,params}) {
  let jobs=[];
  let reports=[];
  let timer;
  let jobsSearchTimer,reportsSearchTimer;
  const pageSize=50;
  let jobsOffset=0,reportsOffset=0,jobsSequence=0,reportsSequence=0;
  let jobsPage={},reportsPage={};
  let detailCleanup;
  let quarantine=null;
  let selectedJob=params.get('job') || '';
  let currentTab=params.get('tab')==='reports'?'reports':'jobs';
  host.innerHTML=heading('任务与报告','所有长计算显式接单，取消状态可追溯，保存报告可独立重开。',`<button class="button" data-refresh-tasks>刷新列表</button>`)+`<div class="segmented" aria-label="任务与报告视图" style="margin-bottom:18px"><button data-tab="jobs" aria-pressed="${currentTab==='jobs'}">任务记录</button><button data-tab="reports" aria-pressed="${currentTab==='reports'}">历史报告</button></div><section class="panel" data-jobs-panel><div class="panel-header"><div><h2>计算任务</h2><p data-jobs-summary>读取任务索引</p></div><span class="badge teal">活动任务自动刷新</span></div><div class="filter-bar"><label class="field-inline">类型<select data-kind-filter><option value="">全部类型</option><option value="analysis">结构分析</option><option value="scan">扫描</option><option value="backtest">回测</option><option value="prediction">预测</option><option value="scan_explanation">指标解释附件</option><option value="kronos_replay">Kronos 逐日验证</option><option value="storage_usage">本地空间盘点</option><option value="factor_training">统一模型训练</option><option value="model_refit">最终重拟合</option><option value="model_scan">模型选股</option><option value="model_backtest">模型回测</option><option value="update">更新</option><option value="import">导入</option></select></label><label class="field-inline">状态<select data-status-filter><option value="">全部状态</option><option value="active">活动</option><option value="succeeded">已完成</option><option value="partial">部分完成</option><option value="failed">失败</option><option value="blocked">已阻止</option><option value="cancelled">已取消</option></select></label><input data-job-search placeholder="任务 ID 或股票" aria-label="搜索任务"></div><div data-jobs-table><div class="loading-state">正在读取任务…</div></div></section><section class="panel" data-reports-panel hidden><div class="panel-header"><h2>保存的研究报告</h2><span class="small muted">读取不重新运行</span></div><div class="filter-bar"><input data-report-search placeholder="运行 ID、类型或股票" aria-label="搜索保存报告"></div><div data-reports-table></div></section><section class="panel space-top" data-job-detail hidden><div class="panel-header"><h2>任务详情</h2><button class="small-button" data-close-detail>收起</button></div><div data-job-detail-content></div></section>`;
  host.querySelector('[data-kind-filter]').insertAdjacentHTML('beforeend','<option value="research_study">十年研究</option><option value="fusion_scan">多因子与Kronos复核</option><option value="trade_plan">次日买卖计划</option>');
  host.insertAdjacentHTML('beforeend','<section class="panel space-top" data-quarantine-panel><div class="panel-header"><div><h2>失败构建目录</h2><p>只读预览；勾选后需二次确认才会删除临时构建目录</p></div><button class="small-button" data-refresh-quarantine>刷新预览</button></div><div class="panel-body" data-quarantine-state><div class="loading-state">正在读取失败构建目录…</div></div><div data-quarantine-result></div></section>');
  function tab(value){currentTab=value;host.querySelector('[data-jobs-panel]').hidden=value!=='jobs';host.querySelector('[data-reports-panel]').hidden=value!=='reports';host.querySelectorAll('[data-tab]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.tab===value)));}
  tab(currentTab);
  host.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>tab(button.dataset.tab),{signal}));
  bindReportLinks(host.querySelector('[data-reports-panel]'),signal);
  for(const name of ['jobs','reports'])host.querySelector(`[data-${name}-table]`).insertAdjacentHTML('afterend',`<div class="panel-body button-row" aria-label="${name==='jobs'?'任务':'报告'}分页"><button type="button" class="small-button" data-${name}-previous>上一页</button><span class="small muted" data-${name}-page></span><button type="button" class="small-button" data-${name}-next>下一页</button></div>`);
  function renderPagination(name,page,offset,count){
    const total=page.total ?? count;
    host.querySelector(`[data-${name}-page]`).textContent=page.legacy_count===undefined?`第 ${Math.floor(offset/pageSize)+1} 页 · ${number(total,0)} 条匹配记录`:`服务未加载历史分页接口，当前仅最近 ${number(page.legacy_count,0)} 条；当前窗口匹配 ${number(total,0)} 条，重启后生效。`;
    host.querySelector(`[data-${name}-previous]`).disabled=offset===0||page.legacy_count!==undefined;
    host.querySelector(`[data-${name}-next]`).disabled=!page.has_more;
  }
  function renderJobs(){
    host.querySelector('[data-jobs-summary]').textContent=jobsPage.legacy_count===undefined?`匹配 ${number(jobsPage.total ?? jobs.length,0)} 条 / 本页 ${jobs.length} 条，其中 ${jobs.filter(activeJob).length} 个活动任务`:`仅搜索最近 ${number(jobsPage.legacy_count,0)} 条中的 ${jobs.length} 条匹配记录；完整历史分页待服务重启生效。`;
    host.querySelector('[data-jobs-table]').innerHTML=table([{label:'任务',render:job=>`<button class="row-link" data-open-job="${escapeHtml(jobId(job))}">${escapeHtml(isPipelinePreview(job)||isPipelinePreview(job.result)?'流水线预检':kindLabel(job.kind))}</button><div class="inline-code muted">${escapeHtml(jobId(job).slice(0,25))}</div>`},{label:'状态',render:job=>badge(job.status,reportStatusLabel(job) || reportStatusLabel(job.result))},{label:'进度',render:job=>{const progress=progressValue(job);return `${progress!==null?`<div class="progress"><span style="width:${progress}%"></span></div>`:''}${progressSummary(job)}`;},wrap:true},{label:'创建时间',render:job=>escapeHtml(dateTime(job.created_at))},{label:'范围 / 原因',render:job=>escapeHtml(job.error || job.params?.symbols?.join(', ') || job.params?.symbol || job.spec?.symbol || job.spec?.symbols?.join(', ') || '以冻结任务参数为准'),wrap:true},{label:'操作',render:job=>`<div class="button-row"><button class="small-button" data-open-job="${escapeHtml(jobId(job))}">详情</button>${activeJob(job)?`<button class="small-button danger quiet" data-cancel-job="${escapeHtml(jobId(job))}">取消</button>`:job.run_id?`<button class="small-button" data-open-result="${escapeHtml(jobId(job))}">结果</button>`:''}</div>`}],jobs,{emptyTitle:'没有匹配的任务',emptyDescription:'在研究页面显式提交计算，或调整类型、状态与搜索条件。'});
  }
  function renderReports(){host.querySelector('[data-reports-table]').innerHTML=reportList(reports,{limit:pageSize});renderPagination('reports',reportsPage,reportsOffset,reports.length);}
  async function openJob(id){
    selectedJob=id;
    const panel=host.querySelector('[data-job-detail]'),target=host.querySelector('[data-job-detail-content]');panel.hidden=false;
    try{const job=await api.job(id,signal);if(signal.aborted||selectedJob!==id)return;detailCleanup?.();target.innerHTML=`<div class="panel-body"><div class="button-row"><strong>${escapeHtml(isPipelinePreview(job)||isPipelinePreview(job.result)?'流水线预检':kindLabel(job.kind))}</strong>${badge(job.status,reportStatusLabel(job) || reportStatusLabel(job.result))}${job.result?.status?badge(job.result.status,`产物：${reportStatusLabel(job.result) || statusLabel(job.result.status)}`):''}</div><p class="inline-code muted space-top">${escapeHtml(id)}</p>${progressSummary(job)}${cancellationNotice(job)}${resultErrorSummary(job.result)}${job.error?`<div class="notice error">${escapeHtml(job.error)}</div>`:''}<dl class="detail-list"><div><dt>创建</dt><dd>${escapeHtml(dateTime(job.created_at))}</dd></div><div><dt>最后更新</dt><dd>${escapeHtml(dateTime(job.updated_at || job.finished_at))}</dd></div><div><dt>报告身份</dt><dd>${escapeHtml(job.run_id || job.result?.result_id || '尚未发布')}</dd></div></dl></div>${disclosure('实际参数与执行进度',{params:job.params || job.spec,progress:job.progress,detail:job.progress_detail,owner:job.owner,cancel_requested:job.cancel_requested,cancel_effective:job.cancel_effective,cancel_note:job.cancel_note},true)}<div data-job-saved-result></div>`;if(job.result)detailCleanup=renderReport(target.querySelector('[data-job-saved-result]'),job.result,{id:job.run_id});else if(!activeJob(job))target.querySelector('[data-job-saved-result]').innerHTML=empty('没有已保存的结果',job.error || '任务可能被取消、阻止或在发布结果前中断。');}catch(error){if(error.name!=='AbortError')target.innerHTML=errorHtml(error);}
  }
  async function loadJobs(){
    const sequence=++jobsSequence;
    const values={q:host.querySelector('[data-job-search]').value.trim(),kind:host.querySelector('[data-kind-filter]').value,status:host.querySelector('[data-status-filter]').value,offset:jobsOffset,limit:pageSize};
    try{const value=await api.jobs(signal,values);if(signal.aborted||sequence!==jobsSequence)return;jobs=arrayOf(value,'jobs');jobsPage=value;if(value.total===undefined||typeof value.has_more!=='boolean'){const source=jobs,query=values.q.toLowerCase();jobs=source.filter(job=>(!values.kind||job.kind===values.kind)&&(!values.status||(values.status==='active'?activeJob(job):job.status===values.status))&&(!query||JSON.stringify({id:jobId(job),params:job.params || job.spec}).toLowerCase().includes(query)));jobsOffset=0;jobsPage={total:jobs.length,has_more:false,legacy_count:source.length};}renderJobs();renderPagination('jobs',jobsPage,jobsOffset,jobs.length);}
    catch(error){if(!signal.aborted&&sequence===jobsSequence)host.querySelector('[data-jobs-table]').innerHTML=errorHtml(error);}
  }
  async function loadReports(){
    const sequence=++reportsSequence;
    try{const value=await api.reports(signal,{q:host.querySelector('[data-report-search]').value.trim(),offset:reportsOffset,limit:pageSize});if(signal.aborted||sequence!==reportsSequence)return;reports=arrayOf(value,'reports');reportsPage=value;if(value.total===undefined||typeof value.has_more!=='boolean'){const source=reports,query=host.querySelector('[data-report-search]').value.trim().toLowerCase();reports=source.filter(row=>!query||JSON.stringify(row).toLowerCase().includes(query));reportsOffset=0;reportsPage={total:reports.length,has_more:false,legacy_count:source.length};}renderReports();}
    catch(error){if(!signal.aborted&&sequence===reportsSequence)host.querySelector('[data-reports-table]').innerHTML=errorHtml(error);}
  }
  async function refresh(){
    clearTimeout(timer);
    await Promise.all([loadJobs(),loadReports()]);if(signal.aborted)return;
    if(selectedJob && jobs.find(job=>jobId(job)===selectedJob))await openJob(selectedJob);
    if(jobs.some(activeJob))timer=setTimeout(refresh,4000);
  }
  function renderQuarantine(){
    const target=host.querySelector('[data-quarantine-state]'),items=arrayOf(quarantine,'items');
    if(!items.length){target.innerHTML=empty('没有失败构建目录','上次运行没有残留 .building 临时目录；这里不会自动删除任何内容。');return;}
    target.innerHTML=`<p class="form-note">${escapeHtml(quarantine?.note || '只读预览；确认后才删除')} · 计划 <span class="code-key">${escapeHtml(quarantine?.plan_id || '—')}</span> · 合计 ${number(quarantine?.total_bytes,0)} 字节</p>${table([{label:'选择',render:row=>`<input type="checkbox" data-quarantine-id="${escapeHtml(row.id)}" aria-label="选择失败构建目录 ${escapeHtml(row.id)}">`},{label:'目录',render:row=>`<span class="code-key">${escapeHtml(row.path_rel || row.dir_name || row.id)}</span>`},{key:'kind',label:'类型'},{label:'创建时间',render:row=>escapeHtml(dateTime(row.created_at))},{label:'大小 / 文件',render:row=>`${number(row.bytes,0)} 字节 / ${number(row.file_count,0)} 个`},{label:'关联任务',render:row=>`${escapeHtml(row.job_kind || '—')} ${escapeHtml(row.job_status || '')}${row.job_error?`<div class="small muted">${escapeHtml(row.job_error)}</div>`:''}`,wrap:true}],items,{emptyTitle:'没有失败构建目录'})}<div class="button-row space-top"><button class="small-button danger" data-confirm-quarantine disabled>确认清理</button><span class="muted small" data-quarantine-count>已选 0 个</span><span class="small muted">需二次确认；只删除失败构建的临时目录，不删除报告、模型、账户或原始数据。</span></div>`;
  }
  async function loadQuarantine(){try{const value=await api.quarantinePreview(signal);if(signal.aborted)return;quarantine=value;renderQuarantine();}catch(error){if(error.name!=='AbortError')host.querySelector('[data-quarantine-state]').innerHTML=errorHtml(error);}}
  async function confirmQuarantine(button){
    const ids=[...host.querySelectorAll('[data-quarantine-id]')].filter(input=>input.checked).map(input=>input.dataset.quarantineId);
    if(!ids.length){notify('请先勾选要清理的失败构建目录。','error');return;}
    if(button.dataset.confirming!=='true'){button.dataset.confirming='true';button.textContent='再次点击确认清理';notify('只删除失败构建的临时目录；再次点击才会提交清理。');return;}
    button.dataset.confirming='';button.textContent='确认清理';
    try{const value=await busy(button,()=>api.quarantineConfirm(quarantineConfirmBody(quarantine?.plan_id,ids),signal),'清理中…');if(signal.aborted)return;host.querySelector('[data-quarantine-result]').innerHTML=`<div class="notice space-top">已删除 ${number(arrayOf(value,'deleted').length,0)} 个失败构建目录，释放 ${number(value?.bytes_freed,0)} 字节${value?.at?`；完成时间 ${escapeHtml(dateTime(value.at))}`:''}${value?.audit_event?` · 审计事件 <span class="code-key">${escapeHtml(value.audit_event)}</span>`:''}。</div>`;await loadQuarantine();}catch(error){showError(host,error);}
  }
  host.querySelector('[data-refresh-quarantine]').addEventListener('click',loadQuarantine,{signal});
  host.querySelector('[data-quarantine-state]').addEventListener('change',event=>{if(!event.target.matches('[data-quarantine-id]'))return;const chosen=host.querySelectorAll('[data-quarantine-id]:checked').length,button=host.querySelector('[data-confirm-quarantine]');if(button){button.disabled=!chosen;button.dataset.confirming='';button.textContent='确认清理';}const count=host.querySelector('[data-quarantine-count]');if(count)count.textContent=`已选 ${chosen} 个`;},{signal});
  host.querySelector('[data-quarantine-state]').addEventListener('click',event=>{const button=event.target.closest('[data-confirm-quarantine]');if(button)confirmQuarantine(button);},{signal});
  for(const selector of ['[data-kind-filter]','[data-status-filter]'])host.querySelector(selector).addEventListener('change',()=>{jobsOffset=0;loadJobs();},{signal});
  host.querySelector('[data-job-search]').addEventListener('input',()=>{jobsOffset=0;++jobsSequence;clearTimeout(jobsSearchTimer);jobsSearchTimer=setTimeout(loadJobs,250);},{signal});
  host.querySelector('[data-report-search]').addEventListener('input',()=>{reportsOffset=0;++reportsSequence;clearTimeout(reportsSearchTimer);reportsSearchTimer=setTimeout(loadReports,250);},{signal});
  for(const name of ['jobs','reports'])for(const direction of ['previous','next'])host.querySelector(`[data-${name}-${direction}]`).addEventListener('click',()=>{const step=direction==='next'?pageSize:-pageSize;if(name==='jobs'){jobsOffset=Math.max(0,jobsOffset+step);loadJobs();}else{reportsOffset=Math.max(0,reportsOffset+step);loadReports();}},{signal});
  host.querySelector('[data-refresh-tasks]').addEventListener('click',refresh,{signal});
  host.querySelector('[data-close-detail]').addEventListener('click',()=>{selectedJob='';detailCleanup?.();host.querySelector('[data-job-detail]').hidden=true;},{signal});
  host.querySelector('[data-jobs-table]').addEventListener('click',async event=>{
    const open=event.target.closest('[data-open-job]'),cancel=event.target.closest('[data-cancel-job]'),result=event.target.closest('[data-open-result]');
    if(open)await openJob(open.dataset.openJob);
    if(result)await openJob(result.dataset.openResult);
    if(cancel){try{await busy(cancel,()=>api.cancel(cancel.dataset.cancelJob,signal),'取消中…');notify('取消请求已保存；任务完成退出后更新最终状态。');await refresh();}catch(error){showError(host,error);}}
  },{signal});
  await Promise.all([refresh(),loadQuarantine()]);
  if(selectedJob && !jobs.some(job=>jobId(job)===selectedJob))await openJob(selectedJob);
  return()=>{clearTimeout(timer);clearTimeout(jobsSearchTimer);clearTimeout(reportsSearchTimer);detailCleanup?.();};
}
