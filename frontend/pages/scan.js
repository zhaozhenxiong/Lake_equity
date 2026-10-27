import {api} from '../core/api.js';
import {arrayOf,busy,disclosure,empty,errorHtml,escapeHtml,formValue,heading,notify,showError,symbolsFrom} from '../core/ui.js';
import {submitJob} from '../core/jobs.js';
import {reportList} from '../core/reports.js';
import {bindModelSelector,modelName} from '../core/models.js';
import {releaseSummary} from '../core/study.js';
import {decisionView,explanationCompatible,selectionLinks,selectionPage,sourceCompatible} from '../core/selection.js';
import {explanationHtml,selectionEvidenceHtml,selectionOverviewHtml,selectionRowsHtml} from '../core/selection-view.js';

// Existing callers retain report order; the controller explicitly defaults to descending score.
export const scanPage=selectionPage;
export function scanSpec(form,{activeRelease}={}){
  const scope=formValue(form,'scope'),fixed=formValue(form,'version_source')==='frozen',mode=formValue(form,'scan_mode');
  const spec={scope,symbols:scope==='all'?[]:symbolsFrom(formValue(form,'symbols'),6000)};
  if(formValue(form,'end'))spec.end=formValue(form,'end');
  const universeScope=formValue(form,'universe_scope');
  if(scope==='symbols' && universeScope)spec.universe_scope=universeScope;
  if(fixed){spec.model_id=formValue(form,'model_id');if(!spec.model_id)throw new Error('请选择冻结模型版本。');if(mode!=='scan')throw new Error('Kronos融合绑定阶段发布版本，请选择阶段默认研究版本。');}
  else{if(!activeRelease)throw new Error('请先在模型中心启用默认方法。');spec.release_id=formValue(form,'release_id') || 'active';}
  return {spec,mode};
}

export async function mount(host,{signal,params}){
  let watcher,explanationWatcher,drawerController,result,decisions,selectedModel=null,activeRelease=null,defaults=null,page=1,reportToken=0,decisionToken=0,drawerToken=0,archiveToken=0,archiveOffset=0,archiveHasMore=false,disposed=false,lastFocus=null;
  const explicit=params.has('symbols') || params.get('universe_scope')==='explicit_symbols';
  host.innerHTML=heading(explicit?'指定股票池选股':'全市场模型选股','查看冻结排序与研究建议；数据状态、时效和可执行资格分别核验。')+`
    <section class="panel selection-run"><form id="scan-form">
      <div class="selection-run-main"><div class="selection-method"><span>本次研究方法</span><strong data-scan-default-name>读取默认方法…</strong><small data-scan-default-id>冻结版本详情见下方</small></div>
        <label>研究范围<select name="scope"><option value="all">全市场冻结人口</option><option value="symbols">指定股票池</option></select></label>
        <label>行情目标日<input name="end" type="date" value="${escapeHtml(params.get('end') || '')}" aria-describedby="selection-date-note"></label>
        <label>持仓账户（可选）<select name="account_id"><option value="">不关联持仓</option></select></label><button type="submit" class="primary" data-scan-submit>运行因子排序</button>
      </div><label class="selection-symbols" data-selection-symbols>指定股票代码<input name="symbols" value="${escapeHtml(params.get('symbols') || '')}" placeholder="600519.SH,000001.SZ"></label>
      <p class="selection-note" id="selection-date-note" data-selection-default-date>目标日留空时由后端按已发布行情和交易日历解析，尚未提交任务。</p>
      <details class="selection-method-details"><summary>方法与范围 · 冻结版本、流程、预测预算</summary><div class="form-grid">
        <label>策略版本<select name="version_source"><option value="active">已发布研究版本</option><option value="frozen">明确选择冻结模型</option></select></label>
        <label>选股流程<select name="scan_mode"><option value="scan">因子排序（不运行 Kronos）</option><option value="factor_baseline">Kronos 对照（不改变排序）</option><option value="kronos_filter">Kronos 条件筛选（研究规则）</option></select></label>
        <label class="span-two">冻结模型<select name="model_id" data-model-select><option value="">正在读取模型版本…</option></select></label>
        <label data-fusion-field hidden>初筛候选数<input name="candidate_count" type="number" value="200" min="1" max="200"></label>
        <label data-fusion-field hidden>预测调用预算<input name="max_calls" type="number" value="20" min="1" max="200"></label>
        <label data-fusion-field hidden>预算秒数<input name="budget_seconds" type="number" value="300" min="1" max="1800"></label>
        <label data-fusion-field hidden>预测设备<select name="kronos_device"><option value="cuda">CUDA</option><option value="cpu">CPU</option></select></label>
      </div><p class="selection-note" data-selection-flow-note></p><div data-model-identity></div><div data-scan-release></div></details>
      <input type="hidden" name="release_id" value="${escapeHtml(params.get('release_id') || '')}"><input type="hidden" name="universe_scope" value="${escapeHtml(params.get('universe_scope') || '')}">
    </form><div class="job-inline" data-scan-job hidden></div></section>
    <section class="panel space-top selection-results"><div class="panel-header"><div><h2>冻结扫描结果</h2><p data-scan-caption>等待显式运行或重开已保存报告</p></div><a class="button" data-selection-trading href="#watchlist?tab=trading" hidden>结合持仓查看计划资格</a></div>
      <div data-scan-population></div><div class="selection-decision-state" data-selection-decision-state></div>
      <div class="selection-toolbar"><label>搜索<input data-scan-filter placeholder="代码、名称、原因" aria-label="筛选扫描结果"></label>
        <label>数据状态<select data-scan-status aria-label="评分状态"><option value="scored">已评分</option><option value="">完整人口</option><option value="missing">输入缺失</option><option value="excluded">动态池排除</option><option value="failed">推理失败</option><option value="unprocessed">尚未处理</option><option value="not_in_baseline">未纳入初筛</option></select></label>
        <label>研究建议<select data-selection-advice><option value="">全部建议</option></select></label>
        <label>排序<select data-selection-sort><option value="desc">模型分数 ↓ 从高到低</option><option value="asc">模型分数 ↑ 从低到高</option><option value="original">冻结报告原顺序</option></select></label>
        <label>每页<select data-selection-page-size><option value="20">20 只</option><option value="100">100 只</option></select></label><button type="button" class="small-button" data-selection-reset>重置筛选</button>
      </div><div data-scan-results>${empty('尚无扫描结果','重开保存报告只读取结果，不重新评分或预测。')}</div>
      <div class="table-footer selection-pagination"><button type="button" class="small-button" data-scan-previous disabled>上一页</button><span data-scan-pagination></span><button type="button" class="small-button" data-scan-next disabled>下一页</button></div><div data-scan-identity></div>
    </section>
    <details class="selection-archive space-top"${params.get('report')?'':' open'}><summary>保存的扫描与报告重开</summary><div class="selection-toolbar"><label>类型<select data-selection-archive-kind><option value="scan">因子扫描</option><option value="fusion_scan">Kronos 融合 / 对照</option></select></label><label>历史检索<input data-selection-archive-query placeholder="报告、模型或日期"></label><button type="button" class="small-button" data-refresh-saved>查询 / 刷新</button></div><div data-saved-scans></div><div class="table-footer"><button type="button" data-selection-archive-prev disabled>上一页</button><span data-selection-archive-caption></span><button type="button" data-selection-archive-next disabled>下一页</button></div></details>
    <dialog class="selection-drawer" data-selection-drawer aria-label="个股评分与决策依据"><div data-selection-drawer-content></div></dialog>`;
  const form=host.querySelector('#scan-form'),drawer=host.querySelector('[data-selection-drawer]');
  form.elements.scope.value=explicit?'symbols':'all';form.elements.version_source.value=params.get('model_id')?'frozen':'active';
  if(['factor_baseline','kronos_filter'].includes(params.get('scan_mode')))form.elements.scan_mode.value=params.get('scan_mode');
  const alive=()=>!disposed && !signal.aborted,rows=()=>decisions?.rows || arrayOf(result,'rows');
  function methodChanged(){
    const fixed=formValue(form,'version_source')==='frozen',value=fixed?selectedModel:activeRelease;
    host.querySelector('[data-scan-default-name]').textContent=fixed?(selectedModel?modelName(selectedModel.model_name || selectedModel.model_type):'请选择冻结模型'):(activeRelease?.name || '尚无默认方法');
    host.querySelector('[data-scan-default-id]').textContent=value?`信息截止 ${value.information_cutoff || '未返回'} · 版本身份在详情中`:'请在详情核对冻结版本';
  }
  function sourceChanged(){
    const fixed=formValue(form,'version_source')==='frozen';
    if(fixed)form.elements.scan_mode.value='scan';
    for(const option of form.elements.scan_mode.options)option.disabled=fixed && option.value!=='scan';
    const fusion=formValue(form,'scan_mode')!=='scan';
    form.elements.model_id.closest('label').hidden=!fixed;form.elements.model_id.required=fixed;
    host.querySelector('[data-model-identity]').hidden=!fixed;host.querySelector('[data-scan-release]').hidden=fixed;
    host.querySelectorAll('[data-fusion-field]').forEach(el=>{el.hidden=!fusion;});
    host.querySelector('[data-scan-submit]').textContent=fixed?'运行冻结模型排序':fusion?'运行选股与显式预测':'运行因子排序';
    host.querySelector('[data-selection-flow-note]').textContent=fusion?'本次会显式调用 Kronos，遵守调用与时间预算；延期、失败和质量保持可见。对照模式不改变因子排序，条件筛选是未独立验证的研究规则。':'因子排序不会调用预测。原始分数不是上涨概率，不从正负号推断买卖。';
    methodChanged();
  }
  function scopeChanged(){
    const all=formValue(form,'scope')==='all';form.elements.symbols.required=!all;form.elements.symbols.disabled=all;host.querySelector('[data-selection-symbols]').hidden=all;
    if(!all)form.elements.universe_scope.value='explicit_symbols';
    host.querySelector('h1').textContent=all?'全市场模型选股':'指定股票池选股';host.querySelector('[data-scan-submit]').setAttribute('aria-label',`按${all?'全市场冻结人口':'指定股票池'}运行选股`);
  }
  function render(){
    if(!result)return;
    const view=selectionPage(rows(),{query:host.querySelector('[data-scan-filter]').value,status:host.querySelector('[data-scan-status]').value,suggestion:host.querySelector('[data-selection-advice]').value,sort:host.querySelector('[data-selection-sort]').value,page,pageSize:Number(host.querySelector('[data-selection-page-size]').value)});
    page=view.page;host.querySelector('[data-scan-results]').innerHTML=selectionRowsHtml(result,view.rows);
    host.querySelector('[data-scan-pagination]').textContent=`第 ${page} / ${view.pages} 页 · 筛选 ${view.total} / 完整返回 ${view.received} 行`;
    host.querySelector('[data-scan-previous]').disabled=page<=1;host.querySelector('[data-scan-next]').disabled=page>=view.pages;
  }
  function renderContext(){
    host.querySelector('[data-scan-population]').innerHTML=selectionOverviewHtml(result,decisions);
    const advice=host.querySelector('[data-selection-advice]'),previous=advice.value,actions=new Map(rows().map(row=>{const value=decisionView(row);return [value.action,value.label];}));
    advice.innerHTML='<option value="">全部建议</option>'+[...actions].map(([action,label])=>`<option value="${escapeHtml(action)}">${escapeHtml(label)}</option>`).join('');if(actions.has(previous))advice.value=previous;render();
  }
  async function loadDecisions(){
    const token=++decisionToken,report=result,accountId=formValue(form,'account_id');decisions=null;
    const state=host.querySelector('[data-selection-decision-state]');state.textContent='正在只读核验研究建议与时效…';renderContext();
    try{
      if(typeof api.scanDecisions!=='function')throw new Error('研究建议接口未加载；当前仅查看原始冻结评分。');
      const account=accountId?await api.tradingAccount(accountId,signal):null;
      if(!alive() || token!==decisionToken || result!==report)return;
      const value=await api.scanDecisions(report.run_id,account?{account_id:accountId,snapshot_id:account.latest_snapshot_id || account.snapshot?.snapshot_id}:{},signal);
      if(!alive() || token!==decisionToken || result!==report)return;
      if(!sourceCompatible(report,value))throw new Error('研究建议的报告、模型或数据代际与当前结果不一致，未拼接。');
      if((account && (value.account_id!==accountId || value.snapshot_id!==(account.latest_snapshot_id || account.snapshot?.snapshot_id))) || (!account && value.account_id))throw new Error('研究建议的账户快照与当前选择不一致，未拼接。');
      decisions=value;state.textContent='研究建议按本报告冻结规则生成；当前账户仅用于只读复核，不改变原排序。';renderContext();
    }catch(error){if(!alive() || token!==decisionToken || error.name==='AbortError')return;state.innerHTML=`<div class="notice">${escapeHtml(error.status===404?'服务尚未加载研究建议接口；原分数仍可查看，不生成买卖建议。':error.message)}</div>`;renderContext();}
  }
  function closeDrawer(){drawerToken++;drawerController?.abort();drawerController=null;explanationWatcher?.stop();if(drawer.open)drawer.close();lastFocus?.focus();}
  async function readExplanation(report,row,token){
    const target=host.querySelector('[data-selection-explanation]');
    try{
      if(typeof api.scanExplanation!=='function')throw new Error('解释读取接口未加载；原报告分数保持可读。');
      const value=await api.scanExplanation(report.run_id,row.symbol,drawerController?AbortSignal.any([signal,drawerController.signal]):signal);
      if(!alive() || token!==drawerToken || result!==report)return;
      if(value.status==='saved' && !explanationCompatible(report,row.symbol,value))throw new Error('指标解释不属于当前报告、股票或输入代际，未显示。');
      target.innerHTML=explanationHtml(value);
    }catch(error){if(alive() && token===drawerToken && error.name!=='AbortError')target.innerHTML=errorHtml(error);}
  }
  async function openEvidence(symbol,button){
    const report=result,row=rows().find(value=>value.symbol===symbol);if(!row)return;
    const token=++drawerToken;drawerController?.abort();drawerController=new AbortController();explanationWatcher?.stop();lastFocus=button;host.querySelector('[data-selection-drawer-content]').innerHTML=selectionEvidenceHtml(report,row);
    drawer.dataset.symbol=symbol;if(!drawer.open)drawer.showModal();await readExplanation(report,row,token);
  }
  async function showResult(value){
    closeDrawer();result=value?.result || value;decisions=null;page=1;decisionToken++;
    host.querySelector('[data-scan-caption]').textContent=`${result.as_of || result.end || '日期未返回'} · 模型 ${result.model_id || '未返回'} · ${result.run_id || '报告身份未返回'}`;
    const link=host.querySelector('[data-selection-trading]');link.hidden=!result.run_id;link.href=selectionLinks(result,{}, {accountId:formValue(form,'account_id')}).trading;
    const {rows:rawRows,top,...identity}=result;host.querySelector('[data-scan-identity]').innerHTML=disclosure('冻结版本、范围与原报告身份（逐股证据按需读取）',identity);
    renderContext();await loadDecisions();
  }
  async function openReport(id){
    const token=++reportToken;
    try{const value=await api.report(id,signal);if(!alive() || token!==reportToken)return;const query=new URLSearchParams(params);query.set('report',id);history.replaceState(null,'',`#scan?${query}`);await showResult(value);}
    catch(error){if(alive() && token===reportToken)showError(host,error);}
  }
  async function startScan(kind,spec,button){
    const token=++reportToken,target=host.querySelector('[data-scan-job]');target.hidden=false;watcher?.stop();watcher=await submitJob(target,button,kind,spec,{signal,onComplete:async job=>{if(!alive())return;if(token===reportToken){if(job.result)await showResult(job.result);else if(job.run_id)await openReport(job.run_id);if(job.error)showError(host,new Error(job.error));}await loadReports();}});
  }
  async function loadReports(){
    const token=++archiveToken,kind=host.querySelector('[data-selection-archive-kind]').value;
    try{
      const response=await api.reports(signal,{kind,q:host.querySelector('[data-selection-archive-query]').value,limit:30,offset:archiveOffset});if(!alive() || token!==archiveToken)return;
      const saved=arrayOf(response,'reports').filter(row=>['model_scan_v1','fusion_scan_v1'].includes(row.family) || ['scan','fusion_scan','model_scan'].includes(row.kind));host.querySelector('[data-saved-scans]').innerHTML=reportList(saved,{limit:30});
      const paged=Number.isFinite(response.total);archiveHasMore=paged && response.has_more===true;
      host.querySelector('[data-selection-archive-caption]').textContent=paged?`第 ${Math.floor(archiveOffset/30)+1} 页 · 共 ${response.total} 份报告`:`当前窗口 ${saved.length} 份；服务尚未加载历史分页接口`;
      host.querySelector('[data-selection-archive-prev]').disabled=archiveOffset===0;host.querySelector('[data-selection-archive-next]').disabled=!archiveHasMore;
    }catch(error){if(alive() && token===archiveToken)host.querySelector('[data-saved-scans]').innerHTML=errorHtml(error);}
  }
  for(const name of ['version_source','scan_mode'])form.elements[name].addEventListener('change',sourceChanged,{signal});form.elements.scope.addEventListener('change',scopeChanged,{signal});
  form.elements.account_id.addEventListener('change',()=>{if(result){host.querySelector('[data-selection-trading]').href=selectionLinks(result,{}, {accountId:formValue(form,'account_id')}).trading;loadDecisions();}},{signal});
  for(const [selector,event] of [['[data-scan-filter]','input'],['[data-scan-status]','change'],['[data-selection-advice]','change'],['[data-selection-sort]','change'],['[data-selection-page-size]','change']])host.querySelector(selector).addEventListener(event,()=>{page=1;render();},{signal});
  host.querySelector('[data-scan-previous]').addEventListener('click',()=>{page--;render();},{signal});host.querySelector('[data-scan-next]').addEventListener('click',()=>{page++;render();},{signal});
  host.querySelector('[data-selection-reset]').addEventListener('click',()=>{host.querySelector('[data-scan-filter]').value='';host.querySelector('[data-scan-status]').value='scored';host.querySelector('[data-selection-advice]').value='';host.querySelector('[data-selection-sort]').value='desc';page=1;render();},{signal});
  host.querySelector('[data-scan-population]').addEventListener('click',async event=>{const status=event.target.closest('[data-selection-status]'),suggestion=event.target.closest('[data-selection-suggestion]'),resume=event.target.closest('[data-resume-fusion]');if(status){host.querySelector('[data-scan-status]').value=status.dataset.selectionStatus;page=1;render();}if(suggestion){host.querySelector('[data-selection-advice]').value=suggestion.dataset.selectionSuggestion;host.querySelector('[data-scan-status]').value='';page=1;render();}if(resume)try{await startScan('fusion_scan',{resume_id:resume.dataset.resumeFusion,max_calls:Number(formValue(form,'max_calls')),budget_seconds:Number(formValue(form,'budget_seconds'))},resume);}catch(error){showError(host,error);}},{signal});
  host.querySelector('[data-scan-results]').addEventListener('click',event=>{const button=event.target.closest('[data-selection-evidence]');if(button)openEvidence(button.dataset.selectionEvidence,button);},{signal});
  drawer.addEventListener('cancel',event=>{event.preventDefault();closeDrawer();},{signal});
  drawer.addEventListener('click',async event=>{
    const close=event.target.closest('[data-selection-close]'),watch=event.target.closest('[data-selection-addwatch]'),build=event.target.closest('[data-selection-build-explanation]');if(close){closeDrawer();return;}
    if(watch)try{await busy(watch,()=>api.addWatch(watch.dataset.selectionAddwatch,signal));notify('已加入关注；关注列表只保存研究范围。');}catch(error){notify(error.message,'error');}
    if(build){const report=result,row=rows().find(value=>value.symbol===drawer.dataset.symbol),token=drawerToken,target=host.querySelector('[data-selection-explanation-job]');target.hidden=false;try{explanationWatcher=await submitJob(target,build,'scan_explanation',{run_id:report.run_id},{signal,onComplete:async job=>{if(!alive() || result!==report || token!==drawerToken)return;if(job.error){target.innerHTML=errorHtml(new Error(job.error));return;}await readExplanation(report,row,token);}});}catch(error){if(alive() && token===drawerToken)target.innerHTML=errorHtml(error);}}
  },{signal});
  form.addEventListener('submit',async event=>{
    event.preventDefault();try{
      const {spec,mode}=scanSpec(form,{activeRelease}),accountId=formValue(form,'account_id');
      if(mode!=='scan')Object.assign(spec,{mode,candidate_count:Number(formValue(form,'candidate_count')),max_calls:Number(formValue(form,'max_calls')),budget_seconds:Number(formValue(form,'budget_seconds')),kronos:{device:formValue(form,'kronos_device')}});
      if(accountId){const account=await api.tradingAccount(accountId,signal);if(!alive() || formValue(form,'account_id')!==accountId)throw new Error('账户选择已变化，请核对后重新提交。');const target=spec.end || defaults?.target_date;if(!target)throw new Error('尚未解析可用行情日，无法核对账户日期。');if(account.snapshot.as_of!==target)throw new Error(`持仓快照 ${account.snapshot.as_of} 与本次信号目标 ${target} 不一致，请先核对真实同日快照。`);if(mode!=='scan')spec.holding_symbols=account.snapshot.positions.map(row=>row.symbol);}
      await startScan(mode==='scan'?'scan':'fusion_scan',spec,form.querySelector('[data-scan-submit]'));
    }catch(error){showError(host,error);}
  },{signal});
  host.querySelector('[data-refresh-saved]').addEventListener('click',()=>{archiveOffset=0;loadReports();},{signal});host.querySelector('[data-selection-archive-kind]').addEventListener('change',()=>{archiveOffset=0;loadReports();},{signal});
  host.querySelector('[data-selection-archive-query]').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();archiveOffset=0;loadReports();}},{signal});
  host.querySelector('[data-selection-archive-prev]').addEventListener('click',()=>{archiveOffset=Math.max(0,archiveOffset-30);loadReports();},{signal});host.querySelector('[data-selection-archive-next]').addEventListener('click',()=>{if(archiveHasMore){archiveOffset+=30;loadReports();}},{signal});
  host.querySelector('[data-saved-scans]').addEventListener('click',event=>{const button=event.target.closest('[data-report-id]');if(button)openReport(button.dataset.reportId);},{signal});
  sourceChanged();scopeChanged();
  await Promise.allSettled([
    loadReports(),bindModelSelector(host,{signal,selected:params.get('model_id') || '',onChange:model=>{selectedModel=model;methodChanged();}}).catch(error=>showError(host,error)),
    (async()=>{try{if(typeof api.scanDefaults!=='function')return;defaults=await api.scanDefaults(signal);if(alive())host.querySelector('[data-selection-default-date]').textContent=`留空由后端解析为 ${defaults.target_date || '待核验'}；最新已发布行情 ${defaults.latest_published_day || '未返回'}。显式选择日期时不静默退回旧日期。`;}catch(error){if(alive())host.querySelector('[data-selection-default-date]').textContent='默认目标日暂不可读；留空由后端核验，或明确指定日期。';}})(),
    (async()=>{try{const value=await api.activeRelease(signal);if(!alive())return;activeRelease=value.release;const requested=params.get('release_id');if(requested && requested!=='active' && activeRelease?.release_id!==requested){const all=await api.releases(signal);if(!alive())return;activeRelease=arrayOf(all,'releases').find(row=>row.release_id===requested) || null;}host.querySelector('[data-scan-release]').innerHTML=releaseSummary(activeRelease);methodChanged();}catch(error){if(alive())showError(host,error);}})(),
    (async()=>{try{const value=await api.tradingAccounts(signal);if(!alive())return;form.elements.account_id.insertAdjacentHTML('beforeend',arrayOf(value,'accounts').filter(row=>!row.is_fixture).map(row=>`<option value="${escapeHtml(row.account_id)}">${escapeHtml(row.name)}</option>`).join(''));const requested=params.get('account_id');if(requested && [...form.elements.account_id.options].some(option=>option.value===requested))form.elements.account_id.value=requested;}catch(error){if(alive())showError(host,error);}})()
  ]);
  if(alive() && params.get('report'))await openReport(params.get('report'));
  return()=>{disposed=true;reportToken++;decisionToken++;drawerToken++;archiveToken++;drawerController?.abort();watcher?.stop();explanationWatcher?.stop();if(drawer.open)drawer.close();};
}
