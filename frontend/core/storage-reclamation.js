import {api} from './api.js';
import {arrayOf,badge,busy,dateTime,disclosure,empty,errorHtml,escapeHtml,number,table} from './ui.js';
import {storageBytes} from './storage-usage.js';

const reasonCode=value=>typeof value==='string'?value:value?.code || value?.reason || value?.label || value?.message || value?.error || '';
export function storageProtectionReasons(row){return [...new Set(['reasons','blocked_reasons','holds'].flatMap(key=>Array.isArray(row[key])?row[key]:[]).map(reasonCode).filter(Boolean))];}
const reasonLabels={learning_reference:'保留的学习或读取引用',retained_lake_equity_learning_or_read_identity:'保留研究 / read_ref 依赖',recent_generation:'最近版本保留策略',current_generation:'当前发布代际',explicit_hold:'显式保留',missing_file_manifest:'缺少文件清单，未核实大小',unreceipted_generation:'缺少代际凭证',incomplete_batches:'批次尚不完整',not_compacted:'尚未完成压实',young_orphan:'较新的孤立批次，保留核验',references_incomplete:'引用核验不完整',active_job:'存在活动任务',checkpoint_reference:'仍有续算检查点引用',completed_qlib_training_exchange:'已完成训练的 Qlib 交换文件',building_or_checkpoint_reference:'存在构建或续算检查点引用',active_job_or_worker:'仍有任务或 worker 运行',unverified_reference_metadata:'引用元数据未核验',hardlink_set_not_exclusive:'硬链接集合非独占，保留原文件',file_identity_or_allocation_unknown:'文件身份或分配空间未知'};
const capabilityLabels={original_qlib_worker_exchange_reuse:'无法直接复用原 Qlib worker 交换文件',byte_complete_training_audit_of_retired_exchange:'退休交换文件不再提供完整训练字节审计',frozen_model_inference:'冻结模型推理',saved_oos_predictions:'已保存样本外预测',rolling_watchlist_replay:'滚动与关注池历史回放',original_report_bytes:'原报告字节与身份'};
const capabilityText=value=>(Array.isArray(value)?value:[value]).filter(Boolean).map(item=>capabilityLabels[item] || item).join('；');
export const storageReasonText=row=>storageProtectionReasons(row).map(code=>reasonLabels[code] || code).join('；') || (row.status==='protected'?'保留原因待核验':'尚未核准删除');
export function storageLogicalBytes(row){return storageProtectionReasons(row).some(code=>['missing_file_manifest','unreceipted_generation'].includes(code))?null:typeof row.logical_bytes==='number'?row.logical_bytes:null;}
export const retentionExpired=(plan,now=Date.now())=>plan.expired===true || !!plan.expires_at && Number.isFinite(Date.parse(plan.expires_at)) && Date.parse(plan.expires_at)<=now;

export function learningRetentionPage(items,{query='',status='',sort='reclaim',page=1,pageSize=20}={}){
  const term=query.trim().toLowerCase(),filtered=items.filter(row=>(!status || row.status===status) && (!term || `${row.object_id} ${row.run_id || ''} ${row.path_rel || ''} ${storageReasonText(row)}`.toLowerCase().includes(term)));
  const key=sort==='logical'?'logical_bytes':'estimated_reclaim_bytes';
  filtered.sort((a,b)=>{const x=a[key],y=b[key];return x==null || y==null?x==y?String(a.object_id).localeCompare(String(b.object_id)):x==null?1:-1:y-x || String(a.object_id).localeCompare(String(b.object_id));});
  const pages=Math.max(1,Math.ceil(filtered.length/pageSize)),current=Math.min(Math.max(1,page),pages);
  return {rows:filtered.slice((current-1)*pageSize,current*pageSize),page:current,pages,total:filtered.length,received:items.length};
}
export function learningRetentionSelection(items,selected){
  const ids=[...selected],allowed=new Set(items.filter(row=>row.status==='candidate').map(row=>row.object_id));
  if(!ids.length)throw new Error('请先选择精确的训练中间件候选。');
  if(ids.length>200)throw new Error('单次最多选择 200 个精确对象，请分批核验。');
  if(ids.some(id=>!allowed.has(id)))throw new Error('选择包含不再可核验候选的对象，请重新读取清单。');
  return ids;
}
export function learningRetentionConfirmation(plan,values){
  if(!(plan.status==='ready' || plan.can_resume===true && ['cancelled','failed'].includes(plan.status)) || !plan.plan_id || !plan.confirmation_token || !plan.items?.length)throw new Error('计划尚未就绪，请重新核验精确对象。');
  if(retentionExpired(plan))throw new Error('审核计划已过期，请重新核验。');
  if(values.confirmed!==true || values.maintenance_confirmed!==true)throw new Error('请确认精确对象范围与维护条件。');
  return {plan_id:plan.plan_id,confirmation_token:plan.confirmation_token,object_ids:plan.items.map(row=>row.object_id),confirmed:true,maintenance_confirmed:true};
}
export function learningRetentionOverviewHtml(value){
  const items=arrayOf(value,'items'),totals=value.totals || {};
  const unknown=arrayOf(value,'unknown_runs'),errors=[...arrayOf(value,'errors'),...arrayOf(value.references,'errors')];
  return `<div class="storage-reclamation-metrics"><div><span>可核验候选文件</span><strong>${number(totals.candidate_count ?? items.filter(row=>row.status==='candidate').length,0)}</strong></div><div><span>保留文件 / 未核验${value.unknown_runs?'实验':'对象'}</span><strong>${number(totals.protected_count ?? items.filter(row=>row.status==='protected').length,0)} / ${number(totals.unknown_runs ?? totals.unknown_count ?? items.filter(row=>row.status==='unknown').length,0)}</strong></div><div><span>已列候选净释放估算</span><strong>${escapeHtml(storageBytes(totals.estimated_reclaim_bytes))}</strong></div></div><p class="form-note">逻辑大小不是净释放。仍有保留硬链接或链接范围未核实时，不承诺释放全部字节；未知保持未知。候选清单不是删除许可，未核验实验的潜在空间不计入已列候选估算。</p><p class="small muted">${value.generated_at?`清单时间 ${escapeHtml(dateTime(value.generated_at))} · `:''}${value.stale?'清单已陈旧，确认前需重新核验 · ':''}原始输入、共享 dataset、预测、报告、权重及所有续算依赖保持保护；仅限后端声明的训练交换文件。</p>${errors.length?`<div class="notice">部分引用尚未核验：${escapeHtml(errors.map(reasonCode).join('；'))}</div>`:''}${unknown.length?`<details><summary>未核验实验 ${unknown.length} 个，不进入执行清单</summary>${table([{label:'实验',key:'run_id',wrap:true},{label:'未核验原因',key:'reason',wrap:true}],unknown)}</details>`:''}${value.references?disclosure('引用保护范围与原始核验边界',{reference_fingerprint:value.reference_fingerprint,references:value.references}):''}`;
}
function learningItemsHtml(items,selected,{selectable=true,disabled=false}={}){
  items=items.map(row=>({...row,capability_loss:capabilityText(row.capability_loss)}));
  if(!items.length)return empty('没有匹配的对象','没有候选也是有效结果；不能为达到预定回收量放宽保护。');
  return items.map(row=>`<article class="storage-reclamation-item">${selectable?`<label class="storage-reclamation-check"><input type="checkbox" data-learning-object="${escapeHtml(row.object_id)}" aria-label="选择 ${escapeHtml(row.object_id)}"${row.status==='candidate' && !disabled?'':' disabled'}${selected.has(row.object_id)?' checked':''}></label>`:''}<div class="storage-reclamation-object"><strong>${escapeHtml(row.run_id || row.object_id)}</strong><code>${escapeHtml(row.path_rel || row.object_id)}</code><span>${badge(row.status,({candidate:'待核验候选',protected:'保留',unknown:'尚未核验'})[row.status] || row.status)}</span></div><div class="storage-reclamation-sizes"><span>逻辑 ${escapeHtml(storageBytes(storageLogicalBytes(row)))}</span><strong>净释放 ${escapeHtml(storageBytes(row.estimated_reclaim_bytes))}</strong><span>分配 ${escapeHtml(storageBytes(row.allocated_bytes))}</span></div><details><summary>理由与精确身份</summary><p>${escapeHtml(storageReasonText(row))}</p><code>${escapeHtml(row.object_id)}</code>${row.capability_loss?`<p>能力变化：${escapeHtml(Array.isArray(row.capability_loss)?row.capability_loss.join('；'):row.capability_loss)}</p>`:''}</details></article>`).join('');
}
export function learningRetentionPlanHtml(plan){
  const expired=retentionExpired(plan) && !['completed','complete','succeeded','running','executing','cancelling'].includes(plan.status),items=arrayOf(plan,'items'),resumable=plan.can_resume===true && ['cancelled','failed'].includes(plan.status),loss=capabilityText(plan.capability_loss || [])+(plan.preserved_capabilities?.length?`；保留能力：${capabilityText(plan.preserved_capabilities)}`:''),ready=(plan.status==='ready' || resumable) && !expired && plan.confirmation_token && items.length;
  const journal=plan.journal || {};
  const {confirmation_token,...evidence}=plan;
  const progress=plan.progress || {},phaseLabels={verify_report_stream:'核验原始报告内容',verify_building_benchmark:'核验构建与基准证据',verify_exchange_sha256:'核验交换文件 SHA256',retire_verified_exchange:'处理已核验的精确对象'};
  const progressHtml=progress.phase?`<div class="notice" role="status"><strong>${escapeHtml(phaseLabels[progress.phase] || progress.phase)}</strong>${progress.bytes!==undefined?`<p>当前核验流已读取 ${escapeHtml(storageBytes(progress.bytes))}</p>`:''}${progress.current!==undefined?`<p>原计划处理位置 ${number(progress.current,0)} / ${number(progress.total,0)}</p>`:''}${progress.path || progress.run_id?`<p class="inline-code">${escapeHtml(progress.path || progress.run_id)}</p>`:''}</div>`:'';
  const actions=`${plan.can_cancel===true?'<button type="button" class="small-button" data-learning-cancel>请求在安全边界停止，已删除内容不可撤销</button>':''}${plan.can_reconcile===true?'<form data-learning-reconcile><p class="notice">原记录存在删除意图但审计未完整落定。此动作只核对并补记已发生的删除，不移除新文件。</p><label class="check-field"><input type="checkbox" name="maintenance_confirmed" required>允许进入维护，核对原计划并补齐已发生删除的审计。</label><button type="submit" class="small-button space-top">仅核对并补记原计划审计</button></form>':''}`;
  return `<h3>精确训练中间件计划</h3><p>${badge(expired?'blocked':plan.status,expired?'计划已过期':({ready:'等待最终确认',executing:'正在执行',running:'正在执行',complete:'已完成',completed:'已完成',cancelled:'已停止',failed:'执行失败，先核对记录',error:'执行失败，先核对记录',partial:'部分完成'})[plan.status] || plan.status)} · <code>${escapeHtml(plan.plan_id || '尚未形成')}</code></p>${plan.error?errorHtml(new Error(plan.error)):''}${['error','failed','partial','cancelled'].includes(plan.status) || journal.status==='error'?'<p class="notice error">可能已有部分文件被移除；不要创建扩大范围的新计划。先读取原记录，再按原计划核验恢复。</p>':''}<p>精确对象 ${number(items.length,0)} 个${typeof plan.removed_count==='number'?` · 已记账移除 ${number(plan.removed_count,0)} 个`:''} · 原计划净释放估算 ${escapeHtml(storageBytes(plan.estimated_reclaim_bytes))}</p>${plan.expires_at?`<p data-learning-expiry>确认截止 ${escapeHtml(dateTime(plan.expires_at))}${expired?' · 已过期，请重新核验':''}</p>`:''}${loss.length?`<p class="notice">能力变化：${escapeHtml(Array.isArray(loss)?loss.join('；'):loss)}</p>`:''}${learningItemsHtml(items,new Set(),{selectable:false})}${ready?`<form data-learning-confirm><label class="check-field"><input type="checkbox" name="confirmed" required>我确认只处理这份计划列出的精确对象，已完成的删除不可撤销。</label><label class="check-field"><input type="checkbox" name="maintenance_confirmed" required>我已停止外部读取与调度，允许进入受控维护；后台仍会重验活动任务与引用。</label><button type="submit" class="danger space-top">${resumable?'确认按原计划继续剩余对象':'确认执行此训练中间件计划'}</button></form>`:''}${Object.keys(journal).length?disclosure('原计划逐项执行与恢复记录',journal,true):''}${(plan.status==='ready' || plan.can_resume) && !plan.confirmation_token?'<p class="notice">这是已保存计划的只读重开，未携带一次性确认令牌。请重新核验精确对象后再确认。</p>':''}${progressHtml}${actions}${disclosure('冻结审核证据',evidence)}`;
}

export function storageReviewHtml(result,confirmEnabled){
  const expired=retentionExpired(result);
  return `<h3>存储核验结果</h3><p>${badge(expired?'blocked':result.status,expired?'计划已过期':undefined)} · 计划 ${escapeHtml(result.plan_id || '尚未形成')}</p>${result.error?errorHtml(new Error(result.error)):''}${table([{key:'object_id',label:'精确对象 ID',wrap:true},{key:'label',label:'对象'},{label:'逻辑字节',render:row=>storageLogicalBytes(row)===null?'未核实':number(storageLogicalBytes(row),0)}],arrayOf(result,'objects'))}<p>本计划总逻辑字节：${number(result.logical_bytes,0)}；不是已核准净释放空间。</p>${result.expires_at?`<p data-storage-expiry>确认截止 ${escapeHtml(dateTime(result.expires_at))}${expired?' · 已过期，重新核验':''}</p>`:''}${result.status==='error'?'<p class="notice">检查失败时不会删除；若在执行阶段失败，可能已删除部分内容，请核对原计划记录。</p>':''}${confirmEnabled && arrayOf(result,'objects').length>0 && !expired && result.status==='ready' && result.job_id && result.confirmation_token?`<form data-storage-confirm><label class="check-field"><input type="checkbox" name="confirmed" required>我确认仅处理以上精确对象和字节范围。</label><label class="check-field"><input type="checkbox" name="maintenance_confirmed" required>我已停止外部读取、服务和调度，允许进入受控维护。</label><button type="submit" class="danger space-top">确认执行此维护计划</button></form>`:''}${disclosure('核验与执行完整证据',result)}`;
}

export function mountLearningReclamation(host,{signal,planId=''}={}){
  let value=null,plan=null,page=1,sequence=0,planSequence=0,selectionRevision=0,disposed=false,pollTimer,expiryTimer;
  const selected=new Set(),tokens=new Map(),controller=new AbortController(),requestSignal=AbortSignal.any([signal,controller.signal]);
  host.innerHTML=`<section class="storage-reclamation"><div class="panel-header"><div><h3>学习文件空间回收</h3><p>只读候选与保留依赖；仅精确审核后才可最终确认</p></div><button type="button" class="small-button" data-learning-reload>重新读取候选</button></div><div data-learning-errors role="alert"></div><div data-learning-overview><div class="loading-state">读取候选与引用保护…</div></div><div class="filter-bar"><label class="field-inline">搜索<input data-learning-query placeholder="实验、对象或原因"></label><label class="field-inline">状态<select data-learning-status><option value="">全部</option><option value="candidate">待核验候选</option><option value="protected">保留</option><option value="unknown">尚未核验</option></select></label><label class="field-inline">排序<select data-learning-sort><option value="reclaim">预计净释放 ↓</option><option value="logical">逻辑大小 ↓</option></select></label></div><div data-learning-items></div><div class="table-footer"><button type="button" data-learning-prev disabled>上一页</button><span data-learning-page></span><button type="button" data-learning-next disabled>下一页</button></div><div class="button-row space-top"><span data-learning-selected>未选择对象</span><button type="button" class="primary" data-learning-review disabled>核验所选精确对象</button></div><p class="form-note">核验会保存独立审核记录，不删除源文件。选项、引用或文件变化时计划失效；当前共享数据集与模型权重不参与此按钮。</p><div data-learning-plan></div></section>`;
  const errors=host.querySelector('[data-learning-errors]'),planHost=host.querySelector('[data-learning-plan]'),alive=()=>!disposed && !signal.aborted;
  function failure(error){if(alive() && error.name!=='AbortError')errors.innerHTML=errorHtml(error);}
  function render(){
    if(!value)return;
    const active=['queued','checking','executing','running','cancelling'].includes(plan?.status),finished=['complete','completed','succeeded','partial','error','failed','cancelled'].includes(plan?.status);
    const view=learningRetentionPage(arrayOf(value,'items'),{query:host.querySelector('[data-learning-query]').value,status:host.querySelector('[data-learning-status]').value,sort:host.querySelector('[data-learning-sort]').value,page});page=view.page;
    host.querySelector('[data-learning-items]').innerHTML=learningItemsHtml(view.rows,selected,{disabled:active || finished});host.querySelector('[data-learning-page]').textContent=`第 ${page} / ${view.pages} 页 · 筛选 ${view.total} / 返回 ${view.received} 个`;
    host.querySelector('[data-learning-prev]').disabled=page<=1;host.querySelector('[data-learning-next]').disabled=page>=view.pages;
    host.querySelector('[data-learning-selected]').textContent=finished?'本批已结束，请读取最新候选后核验下一批':selected.size?`已选 ${selected.size} 个；单次最多 200 个`:'未选择对象';host.querySelector('[data-learning-review]').disabled=active || finished || !selected.size;host.querySelector('[data-learning-reload]').disabled=active;
  }
  function expire(){
    clearTimeout(expiryTimer);if(!plan || !alive() || !planHost.querySelector('[data-learning-confirm]') || !plan.expires_at)return;
    if(retentionExpired(plan)){planHost.querySelector('[data-learning-confirm] button')?.setAttribute('disabled','');const note=planHost.querySelector('[data-learning-expiry]');if(note)note.textContent='计划已过期，请重新核验所选精确对象。';return;}
    expiryTimer=setTimeout(expire,Math.min(1000,Math.max(1,Date.parse(plan.expires_at)-Date.now())));
  }
  function showPlan(next){
    if(next.confirmation_token)tokens.set(next.plan_id,next.confirmation_token);
    else if(tokens.has(next.plan_id))next={...next,confirmation_token:tokens.get(next.plan_id)};
    plan=next;planHost.innerHTML=learningRetentionPlanHtml(next);expire();render();
    planHost.querySelector('[data-learning-confirm]')?.addEventListener('submit',async event=>{
      event.preventDefault();const frozen=plan,revision=selectionRevision;
      try{const values=new FormData(event.currentTarget),body=learningRetentionConfirmation(frozen,{confirmed:values.has('confirmed'),maintenance_confirmed:values.has('maintenance_confirmed')});if(plan!==frozen || revision!==selectionRevision)throw new Error('选择已变化，请重新核验。');errors.replaceChildren();const result=await busy(event.currentTarget.querySelector('[type=submit]'),()=>api.learningRetentionConfirm(body,requestSignal),'提交最终确认…');if(!alive())return;await readPlan(result.plan_id || frozen.plan_id);}
      catch(error){failure(error);}
    },{signal:requestSignal});
    planHost.querySelector('[data-learning-cancel]')?.addEventListener('click',async event=>{const id=plan.plan_id;try{await busy(event.currentTarget,()=>api.learningRetentionCancel(id,requestSignal),'请求安全停止…');if(alive())await readPlan(id);}catch(error){failure(error);}},{signal:requestSignal});
    planHost.querySelector('[data-learning-reconcile]')?.addEventListener('submit',async event=>{event.preventDefault();const id=plan.plan_id;try{if(!new FormData(event.currentTarget).has('maintenance_confirmed'))throw new Error('请先确认维护核对条件。');await busy(event.currentTarget.querySelector('[type=submit]'),()=>api.learningRetentionReconcile(id,{maintenance_confirmed:true},requestSignal),'核对原审计…');if(alive())await readPlan(id);}catch(error){failure(error);}},{signal:requestSignal});
  }
  async function readPlan(id){
    clearTimeout(pollTimer);const token=++planSequence,revision=selectionRevision;
    try{const next=await api.learningRetentionPlan(id,requestSignal);if(!alive() || token!==planSequence || revision!==selectionRevision)return;if(next.plan_id!==id)throw new Error('读取到另一份回收计划，未显示。');showPlan(next);if(['queued','checking','executing','running','cancelling'].includes(next.status))pollTimer=setTimeout(()=>readPlan(id),1500);}
    catch(error){failure(error);}
  }
  async function load(){
    const token=++sequence;planSequence++;selectionRevision++;selected.clear();tokens.clear();clearTimeout(pollTimer);clearTimeout(expiryTimer);plan=null;value=null;planHost.replaceChildren();errors.replaceChildren();host.querySelector('[data-learning-items]').replaceChildren();host.querySelector('[data-learning-selected]').textContent='未选择对象';host.querySelector('[data-learning-review]').disabled=true;host.querySelector('[data-learning-prev]').disabled=true;host.querySelector('[data-learning-next]').disabled=true;host.querySelector('[data-learning-overview]').innerHTML='<div class="loading-state">读取候选与引用保护…</div>';
    try{const next=await api.learningRetentionCandidates(requestSignal);if(!alive() || token!==sequence)return;value=next;host.querySelector('[data-learning-overview]').innerHTML=learningRetentionOverviewHtml(value);render();if(planId){const id=planId;planId='';await readPlan(id);}}
    catch(error){if(!alive() || token!==sequence || error.name==='AbortError')return;host.querySelector('[data-learning-overview]').innerHTML=errorHtml(error.status===404?new Error('服务尚未加载学习文件回收接口；现有湖代际核验仍可使用，不展示虚构候选。'):error);}
  }
  for(const [selector,event] of [['[data-learning-query]','input'],['[data-learning-status]','change'],['[data-learning-sort]','change']])host.querySelector(selector).addEventListener(event,()=>{page=1;render();},{signal:requestSignal});
  host.querySelector('[data-learning-items]').addEventListener('change',event=>{const input=event.target.closest('[data-learning-object]');if(!input)return;selectionRevision++;planSequence++;if(input.checked)selected.add(input.dataset.learningObject);else selected.delete(input.dataset.learningObject);plan=null;clearTimeout(pollTimer);clearTimeout(expiryTimer);planHost.replaceChildren();render();},{signal:requestSignal});
  host.querySelector('[data-learning-review]').addEventListener('click',async event=>{
    const revision=selectionRevision,token=sequence,planToken=++planSequence;
    try{const object_ids=learningRetentionSelection(arrayOf(value,'items'),selected);errors.replaceChildren();plan=null;planHost.replaceChildren();clearTimeout(pollTimer);clearTimeout(expiryTimer);const next=await busy(event.currentTarget,()=>api.learningRetentionReview({object_ids},requestSignal),'精确核验中…');if(!alive() || token!==sequence || revision!==selectionRevision || planToken!==planSequence)return;showPlan(next);planHost.scrollIntoView({block:'start'});const query=new URLSearchParams(location.hash.split('?')[1] || '');query.set('tab','operations');query.set('ops','storage');query.set('retention_plan',next.plan_id);history.replaceState(null,'',`#data?${query}`);}
    catch(error){failure(error);}
  },{signal:requestSignal});
  host.querySelector('[data-learning-reload]').addEventListener('click',load,{signal:requestSignal});host.querySelector('[data-learning-prev]').addEventListener('click',()=>{page--;render();},{signal:requestSignal});host.querySelector('[data-learning-next]').addEventListener('click',()=>{page++;render();},{signal:requestSignal});load();
  return()=>{disposed=true;sequence++;controller.abort();clearTimeout(pollTimer);clearTimeout(expiryTimer);};
}
