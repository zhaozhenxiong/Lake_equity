import {api} from './api.js';
import {arrayOf,badge,busy,disclosure,empty,errorHtml,escapeHtml,formValue,number,routeLink,showError,table,today,dateTime,jobId} from './ui.js';
import {submitJob,watchJob} from './jobs.js';

const snapshotKeys=['as_of','cash','positions','permissions','source','is_fixture','last_rebalance_date','note'];
export function cleanSnapshot(value){return Object.fromEntries(snapshotKeys.filter(key=>value[key]!==undefined).map(key=>[key,value[key]]));}
export function accountSnapshot(form){
  if(formValue(form,'source_mode')==='json'){
    let value;try{value=JSON.parse(formValue(form,'snapshot_json'));}catch{throw new Error('账户JSON格式不正确。');}
    value=value.snapshot || value;if(!value || typeof value!=='object' || !Array.isArray(value.positions))throw new Error('JSON须包含as_of、cash和positions数组。');
    return cleanSnapshot(value);
  }
  const positions=[...form.querySelectorAll('[data-position-row]')].map(row=>{const get=name=>row.querySelector(`[data-position=${name}]`).value,value={symbol:get('symbol').trim().toUpperCase(),total_shares:Number(get('total_shares')),sellable_shares:Number(get('sellable_shares')),frozen_shares:Number(get('frozen_shares') || 0)};if(get('average_cost'))value.average_cost=Number(get('average_cost'));if(get('buy_date'))value.buy_date=get('buy_date');else if(row.dataset.buyLots)value.buy_lots=JSON.parse(row.dataset.buyLots);return value;});
  return {as_of:formValue(form,'as_of'),cash:Number(formValue(form,'cash')),positions,permissions:{star:form.elements.star.checked,chinext:form.elements.chinext.checked,risk_warning:form.elements.risk_warning.checked},source:'dashboard_manual_import',is_fixture:form.dataset?.isFixture==='true'};
}

export function planReport(plan){
  return `<div class="panel-body">${plan.is_fixture?'<div class="notice">合成验收账户：此计划只验证软件，不是你的真实持仓。</div>':''}<h3>次日条件计划 · ${escapeHtml(plan.execution_date)}</h3><p class="code-key">${escapeHtml(plan.plan_id)}</p><dl class="detail-list"><div><dt>信号日 / 执行日</dt><dd>${escapeHtml(plan.signal_date)} / ${escapeHtml(plan.execution_date)}</dd></div><div><dt>选股来源</dt><dd>${plan.source_kind==='model_scan_v1'?'因子选股':plan.source_kind==='fusion_scan_v1'||plan.fusion_id?'融合/对照选股':'来源类型未返回'} <span class="code-key">${escapeHtml(plan.source_id||plan.fusion_id||'—')}</span></dd></div><div><dt>快照现金 / 预估卖出所得</dt><dd>${number(plan.cash)} / ${number(plan.estimated_sale_proceeds)} 元</dd></div><div><dt>参考权益 / 待核验估值</dt><dd>${number(plan.account_equity_reference)} 元 / ${(plan.valuation_missing_symbols || []).map(escapeHtml).join('、') || '无'}</dd></div><div><dt>调仓是否到期</dt><dd>${plan.rebalance_due?'是':'否，仍评估风险退出'}</dd></div></dl><p class="form-note">以下为按信号收盘价计算的条件数量和限价；依赖卖出所得的买单须等真实卖出确认。计划不连接券商，不等于成交。</p></div>${table([{key:'symbol',label:'股票'},{key:'side',label:'方向'},{label:'计划股数',render:r=>number(r.quantity,0)},{label:'可卖股数',render:r=>number(r.sellable_shares,0)},{label:'条件限价',render:r=>number(r.limit_price,3)},{label:'预计费用',render:r=>number(r.estimated_fees)},{key:'funding',label:'资金来源',wrap:true},{label:'依赖与限制',render:r=>escapeHtml([...(r.depends_on_orders || []),...(r.constraints || [])].join('；')),wrap:true}],plan.orders || [],{emptyTitle:'本次没有拟交易订单',emptyDescription:'可能未到调仓日、无合格候选、资金或交易权限不足。保留下面的持仓与等待原因。'})}${table([{key:'symbol',label:'保留或等待股票'},{key:'action',label:'动作'},{key:'reason',label:'原因',wrap:true}],plan.holds || [])}${disclosure('冻结发布、账户、风险、有效期与完整计划',plan)}`;
}

export async function mountTrading(host,{signal,params}){
  let watcher,account=null,plan=null,accounts=[],plans=[],selections=[],preview=null,disposed=false,submitting=false;
  let accountTicket=0,refreshTicket=0,previewTicket=0,planTicket=0,jobTicket=0;
  let selectedAccount=params.get('account_id')||'',selectedSource=params.get('source_id')||params.get('fusion_id')||'';
  let restoreFixtureAccount=selectedAccount;
  const alive=()=>!disposed&&!signal.aborted&&host.isConnected;
  host.innerHTML=`<section class="panel space-top"><div class="panel-header"><div><h2>持仓快照与次日计划</h2><p>账户独立于关注列表；只保存你录入的现金、持仓和实际成交</p></div><button class="small-button" data-refresh-trading>刷新账户与计划</button></div><div class="panel-body"><div class="form-grid"><label class="span-two">当前账户<select data-trading-account><option value="">选择账户或新建账户</option></select></label><label class="check-field"><input type="checkbox" data-show-fixtures>显示合成验收账户</label></div><div data-account-error data-page-error></div><div data-current-account>${empty('尚未选择持仓账户','可手动录入或导入自己的账户快照；不会从关注列表生成持仓。')}</div></div><details class="panel-body" open><summary>录入或更新账户快照</summary><form data-snapshot-form class="form-grid space-top"><label>账户名称<input name="name" placeholder="填写自己的账户名称" required></label><label>录入方式<select name="source_mode"><option value="manual">手工填写</option><option value="json">JSON模板导入</option></select></label><label data-manual-field>快照日期<input type="date" name="as_of" value="${today()}" required></label><label data-manual-field>可用现金（元）<input type="number" name="cash" min="0" step="0.01" required></label><fieldset data-manual-field class="model-checks span-all"><legend>已开通交易权限</legend><label><input type="checkbox" name="star">科创板</label><label><input type="checkbox" name="chinext">创业板</label><label><input type="checkbox" name="risk_warning">风险警示股票</label></fieldset><div data-manual-field class="span-all"><div class="button-row"><button type="button" data-add-position>添加持仓行</button><span class="small muted">未填入持仓行表示空仓；可卖数与当日买入日期须真实填写。</span></div><div data-position-rows></div></div><div data-json-field class="span-all" hidden><div class="button-row"><button type="button" data-load-template>读取空白JSON模板</button><label class="field-inline">导入文件<input type="file" accept=".json,application/json" data-import-snapshot></label></div><label>完整账户快照JSON<textarea name="snapshot_json" rows="14" spellcheck="false" placeholder="从空白模板填写真实数据"></textarea></label></div><div class="button-row span-all"><button type="submit" class="primary">保存为新账户</button><button type="button" data-save-snapshot disabled>为当前账户保存新快照</button></div></form></details></section><section class="panel space-top"><div class="panel-header"><h2>从已保存选股构建计划</h2></div><form data-plan-form class="form-toolbar"><div class="form-grid"><label class="span-two">已保存选股来源<select name="source_id" required><option value="">正在读取选股报告…</option></select></label><button type="submit" class="primary" disabled>构建次日计划</button></div><p class="form-note">持仓快照日期须与选股信号日一致，并且所有持仓均须纳入该次复核。预算延期和失败保持可见。</p><a class="button" data-account-scan href="#scan">为当前账户重新选股与持仓复核</a></form><div class="panel-body" data-plan-preflight></div><div class="panel-body" data-plan-error data-page-error></div><div class="job-inline" data-plan-job hidden></div><div data-plan-result></div></section><section class="panel space-top"><div class="panel-header"><h2>已保存计划</h2></div><div data-trading-plans></div></section><section class="panel space-top" data-fill-section hidden><div class="panel-header"><h2>回填实际成交</h2></div><div class="panel-body"><p>仅填写已发生的成交及实际费用；卖出先于依赖其资金的买入。相同成交号重复提交会校验并去重。</p><form data-fill-form><div class="button-row"><button type="button" data-fill-template>生成待填写成交模板</button></div><label class="space-top">实际成交JSON<textarea name="fills_json" rows="10" spellcheck="false" required placeholder='{"fills":[]}'></textarea></label><button class="primary space-top" type="submit">核验成交并追加账户快照</button></form><div data-fill-error data-page-error></div><div data-fill-result></div></div></section>`;
  function positionRow(value={}){const div=document.createElement('div');div.className='holding-input-row form-grid space-top';div.dataset.positionRow='';if(value.buy_lots)div.dataset.buyLots=JSON.stringify(value.buy_lots);div.innerHTML=[['symbol','股票代码','text'],['total_shares','总股数','number'],['sellable_shares','可卖股数','number'],['frozen_shares','冻结股数','number'],['average_cost','平均成本（可选）','number'],['buy_date','买入日期（可选）','date']].map(([key,label,type])=>`<label>${label}<input data-position="${key}" type="${type}" value="${escapeHtml(value[key] ?? (key==='frozen_shares'?0:''))}"${['symbol','total_shares','sellable_shares'].includes(key)?' required':''}${type==='number'?' min="0" step="any"':''}></label>`).join('')+'<button type="button" class="small-button danger" data-remove-position>移除此行</button>';host.querySelector('[data-position-rows]').append(div);}
  function sourceChanged(){const json=formValue(form,'source_mode')==='json';host.querySelectorAll('[data-manual-field]').forEach(el=>{el.hidden=json;});host.querySelector('[data-json-field]').hidden=!json;form.querySelector('[name=cash]').required=!json;form.querySelector('[name=as_of]').required=!json;host.querySelectorAll('[data-position-row] input[required]').forEach(el=>{el.disabled=json;});form.querySelector('[name=snapshot_json]').required=json;}
  function renderAccount(){form.dataset.isFixture=String(account?.is_fixture===true);host.querySelector('[data-save-snapshot]').disabled=!account;host.querySelector('[data-account-scan]').href=routeLink('scan',{account_id:account?.account_id,scan_mode:'factor_baseline',end:account?.snapshot?.as_of});if(!account)return;const snapshot=account.snapshot;host.querySelector('[data-current-account]').innerHTML=`${account.is_fixture?'<div class="notice">合成验收账户：软件测试数据，不是你的真实账户。</div>':''}<dl class="detail-list"><div><dt>账户 / 最新快照</dt><dd>${escapeHtml(account.name)} / ${escapeHtml(snapshot.as_of)}</dd></div><div><dt>可用现金 / 持仓种类</dt><dd>${number(snapshot.cash)} 元 / ${number(snapshot.positions?.length,0)}</dd></div><div><dt>快照身份</dt><dd class="code-key">${escapeHtml(snapshot.snapshot_id)}</dd></div></dl>${table([{key:'symbol',label:'股票'},{label:'总股数',render:r=>number(r.total_shares,0)},{label:'可卖',render:r=>number(r.sellable_shares,0)},{label:'冻结',render:r=>number(r.frozen_shares,0)},{label:'平均成本',render:r=>number(r.average_cost)}],snapshot.positions || [])}${disclosure('账户原始快照与权限',snapshot)}`;form.querySelector('[name=name]').value=account.name;form.querySelector('[name=as_of]').value=snapshot.as_of;form.querySelector('[name=cash]').value=snapshot.cash;for(const key of ['star','chinext','risk_warning'])form.elements[key].checked=snapshot.permissions?.[key]===true;host.querySelector('[data-position-rows]').replaceChildren();for(const row of snapshot.positions || [])positionRow(row);form.querySelector('[name=snapshot_json]').value=JSON.stringify(cleanSnapshot(snapshot),null,2);renderPlans();}
  function renderPlans(){const show=host.querySelector('[data-show-fixtures]').checked;host.querySelector('[data-trading-plans]').innerHTML=table([{label:'计划',render:r=>`<button class="small-button" data-open-plan="${escapeHtml(r.plan_id)}">${escapeHtml(r.plan_id)}</button>`},{key:'execution_date',label:'执行日'},{label:'订单数',render:r=>number(r.orders?.length,0)},{label:'账户类型',render:r=>r.is_fixture?'合成验收账户':'手工账户'}],plans.filter(r=>(show||!r.is_fixture)&&(!account||r.account_id===account.account_id)),{emptyTitle:'当前没有保存计划',emptyDescription:'先选择真实账户与已保存选股，再构建计划。'});}
  const form=host.querySelector('[data-snapshot-form]'),planForm=host.querySelector('[data-plan-form]');
  const sourceSelect=host.querySelector('[name=source_id]'),planButton=planForm.querySelector('[type=submit]');
  const localError=(kind,error)=>{if(alive())showError(host.querySelector(`[data-${kind}-error]`),error);};
  const clearError=kind=>host.querySelector(`[data-${kind}-error]`)?.replaceChildren();
  function writeUrl(values={}){
    const source=selections.find(row=>row.source_id===selectedSource);
    for(const [key,value] of Object.entries({account_id:selectedAccount,source_id:selectedSource,source_kind:source?.source_kind,...values})){if(value)params.set(key,value);else params.delete(key);}
    params.delete('fusion_id');
    const keys=['tab','account_id','source_id','source_kind','plan_id','plan_job'];
    history.replaceState(null,'',routeLink('watchlist',Object.fromEntries(keys.filter(key=>params.get(key)).map(key=>[key,params.get(key)]))));
  }
  function currentRequest(){const source=selections.find(row=>row.source_id===selectedSource);return account&&selectedAccount===account.account_id&&source?{account_id:account.account_id,snapshot_id:account.latest_snapshot_id,source_id:source.source_id,source_kind:source.source_kind}:null;}
  const sameRequest=(a,b)=>!!a&&!!b&&['account_id','snapshot_id','source_id','source_kind'].every(key=>a[key]===b[key]);
  function updateButton(){planButton.disabled=submitting||preview?.status!=='ready'||!sameRequest(preview?.request,currentRequest());}
  function invalidate({resetPlan=false,stopJob=false}={}){
    ++previewTicket;preview=null;updateButton();
    if(resetPlan){++planTicket;plan=null;host.querySelector('[data-plan-result]').replaceChildren();host.querySelector('[data-fill-section]').hidden=true;params.delete('plan_id');}
    if(stopJob){++jobTicket;watcher?.stop();watcher=null;params.delete('plan_job');}
  }
  function renderPreflight(value){
    const dates=value?.dates||{},source=selections.find(row=>row.source_id===selectedSource),issues=value?.issues||[];
    const rows=[['账户最新快照日',dates.snapshot_as_of||account?.snapshot?.as_of],['所选信号日',dates.signal_date||source?.as_of],['原执行交易日',dates.execution_date],['最新已发布行情日',dates.latest_published_day],['报告生成时间',source?.created_at?dateTime(source.created_at):null]];
    host.querySelector('[data-plan-preflight]').innerHTML=`<dl class="detail-list">${rows.map(([label,value])=>`<div><dt>${label}</dt><dd>${escapeHtml(value||'尚未核验')}</dd></div>`).join('')}</dl><div class="notice${value?.status==='blocked'?' error':''}" role="status">${value?badge(value.status,value.status==='ready'?'账户与来源核验通过':'暂不可构建计划'):'选择账户和已保存选股后进行只读预检'}${issues.map(issue=>`<p>${escapeHtml(issue.message||issue.code)}</p>`).join('')}${(value?.next_actions||[]).map(action=>`<p>${escapeHtml(typeof action==='string'?action:action.message||action.label||'')}</p>`).join('')}</div>${value?.decision_policy?disclosure('冻结策略与核验条件',value.decision_policy):''}<p class="form-note">信号日、账户快照日和报告生成时间分别核对。历史信号可查看；预检不创建任务、计划或修改持仓。</p>`;
  }
  async function preflight(){
    const ticket=++previewTicket,request=currentRequest();preview=null;updateButton();
    if(!request){renderPreflight(null);return null;}
    host.querySelector('[data-plan-preflight]').innerHTML='<div class="loading-state">正在只读核对日期、账户与选股来源…</div>';
    try{
      const value=await api.planPreflight(request,signal);
      if(!alive()||ticket!==previewTicket||!sameRequest(request,currentRequest()))return null;
      if(value.status==='ready'&&(!sameRequest(request,value.identity)||!value.identity?.source_sha256))throw new Error('预检返回的账户或来源身份不一致，请刷新后重试。');
      preview={...value,request};renderPreflight(preview);updateButton();return preview;
    }catch(error){if(alive()&&ticket===previewTicket){localError('plan',error);renderPreflight({status:'blocked',issues:[{message:'预检未完成，请重新选择来源或刷新后重试。'}]});}return null;}
  }
  async function chooseAccount(id,{resetPlan=true,updateUrl=true}={}){
    const ticket=++accountTicket;selectedAccount=id;account=null;invalidate({resetPlan,stopJob:resetPlan});clearError('account');clearError('plan');
    host.querySelector('[data-save-snapshot]').disabled=true;
    if(updateUrl)writeUrl({account_id:id,plan_id:resetPlan?'':params.get('plan_id'),plan_job:resetPlan?'':params.get('plan_job')});
    if(!id){form.reset();form.dataset.isFixture='false';host.querySelector('[data-position-rows]').replaceChildren();form.querySelector('[name=snapshot_json]').value='';sourceChanged();host.querySelector('[data-current-account]').innerHTML=empty('尚未选择持仓账户','保存新账户后可生成计划。');renderPlans();renderPreflight(null);return;}
    host.querySelector('[data-current-account]').innerHTML='<div class="loading-state">正在读取所选账户…</div>';
    try{const value=await api.tradingAccount(id,signal);if(!alive()||ticket!==accountTicket||selectedAccount!==id)return;if(value.account_id!==id)throw new Error('返回的账户身份与当前选择不一致。');account=value;renderAccount();await preflight();}
    catch(error){if(alive()&&ticket===accountTicket){localError('account',error);renderPreflight(null);}}
  }
  async function refresh({reloadAccount=true}={}){
    const ticket=++refreshTicket;
    const results=await Promise.allSettled([api.tradingAccounts(signal),api.tradingSelections(signal),api.tradingPlans(signal)]);
    if(!alive()||ticket!==refreshTicket)return;
    if(results[0].status==='fulfilled'){
      accounts=arrayOf(results[0].value,'accounts');
      const fixtureToggle=host.querySelector('[data-show-fixtures]');
      if(restoreFixtureAccount&&restoreFixtureAccount===selectedAccount&&accounts.some(row=>row.account_id===restoreFixtureAccount&&row.is_fixture))fixtureToggle.checked=true;
      restoreFixtureAccount='';
      const select=host.querySelector('[data-trading-account]'),show=fixtureToggle.checked;
      select.innerHTML='<option value="">选择账户或新建账户</option>'+accounts.filter(row=>show||!row.is_fixture).map(row=>`<option value="${escapeHtml(row.account_id)}">${escapeHtml(row.name)}${row.is_fixture?'（合成验收账户）':''}</option>`).join('');
      if([...select.options].some(row=>row.value===selectedAccount))select.value=selectedAccount;else if(selectedAccount){selectedAccount='';await chooseAccount('');}
    }else localError('account',results[0].reason);
    if(results[1].status==='fulfilled'){
      selections=arrayOf(results[1].value,'selections');sourceSelect.innerHTML='<option value="">选择已保存选股</option>'+selections.map(row=>`<option value="${escapeHtml(row.source_id)}">${escapeHtml(row.as_of||'日期未知')} / ${row.source_kind==='model_scan_v1'?'因子选股':'融合/对照选股'} / ${escapeHtml(row.status)} / ${escapeHtml(row.source_id)}</option>`).join('');
      if([...sourceSelect.options].some(row=>row.value===selectedSource))sourceSelect.value=selectedSource;else{selectedSource='';invalidate();}
    }else{invalidate();localError('plan',results[1].reason);}
    if(results[2].status==='fulfilled'){plans=arrayOf(results[2].value,'plans');renderPlans();}else localError('plan',results[2].reason);
    if(ticket!==refreshTicket||!alive())return;
    if(selectedAccount&&(reloadAccount||!account))await chooseAccount(selectedAccount,{resetPlan:false,updateUrl:false});else await preflight();
  }
  function showPlan(value){plan=value?.result||value;host.querySelector('[data-plan-result]').innerHTML=planReport(plan);host.querySelector('[data-fill-section]').hidden=false;writeUrl({plan_id:plan.plan_id,plan_job:''});}
  async function openPlan(id,{restoreAccount=false}={}){
    const ticket=++planTicket,requestedAccount=selectedAccount;
    try{
      const value=await api.tradingPlan(id,signal);if(!alive()||ticket!==planTicket||requestedAccount!==selectedAccount)return;
      if(restoreAccount&&!selectedAccount&&value.account_id){host.querySelector('[data-trading-account]').value=value.account_id;await chooseAccount(value.account_id,{resetPlan:false});}
      if(!alive()||ticket!==planTicket)return;
      if(selectedAccount&&value.account_id!==selectedAccount)throw new Error('此计划属于其他账户，请先选择对应账户。');
      showPlan(value);
    }catch(error){if(alive()&&ticket===planTicket)localError('plan',error);}
  }
  function completion(token,request){return async job=>{
    if(!alive()||token!==jobTicket||!sameRequest(request,currentRequest()))return;
    writeUrl({plan_job:''});
    if(job.result)showPlan(job.result);
    if(job.error)localError('plan',new Error(job.error));
    await refresh({reloadAccount:false});
  };}
  async function restoreJob(id){
    const token=++jobTicket,request=currentRequest();
    try{const job=await api.job(id,signal);if(!alive()||token!==jobTicket)return;
      const jobParams=job.params||{};
      if(job.kind!=='trade_plan'||!request||jobParams.account_id!==request.account_id||jobParams.snapshot_id!==request.snapshot_id||(jobParams.source_id||jobParams.fusion_id)!==request.source_id)throw new Error('任务身份与当前账户或选股来源不同，请在任务页核对。');
      const target=host.querySelector('[data-plan-job]');target.hidden=false;watcher=watchJob(target,job,{signal,onComplete:completion(token,request)});
    }catch(error){if(alive()&&token===jobTicket)localError('plan',error);}
  }
  host.querySelector('[data-trading-account]').addEventListener('change',event=>chooseAccount(event.target.value),{signal});
  sourceSelect.addEventListener('change',async event=>{selectedSource=event.target.value;invalidate({resetPlan:true,stopJob:true});clearError('plan');const source=selections.find(row=>row.source_id===selectedSource);writeUrl({source_id:selectedSource,source_kind:source?.source_kind||'',plan_id:'',plan_job:''});await preflight();},{signal});
  host.querySelector('[data-show-fixtures]').addEventListener('change',()=>refresh(),{signal});host.querySelector('[data-refresh-trading]').addEventListener('click',()=>refresh(),{signal});
  form.querySelector('[name=source_mode]').addEventListener('change',sourceChanged,{signal});host.querySelector('[data-add-position]').addEventListener('click',()=>positionRow(),{signal});host.querySelector('[data-position-rows]').addEventListener('click',event=>event.target.closest('[data-remove-position]')?.closest('[data-position-row]').remove(),{signal});
  form.addEventListener('submit',async event=>{event.preventDefault();const ticket=accountTicket;clearError('account');try{const saved=await busy(form.querySelector('[type=submit]'),()=>api.createTradingAccount({name:formValue(form,'name'),snapshot:accountSnapshot(form)},signal),'保存中…');if(!alive())return;if(ticket!==accountTicket){await refresh({reloadAccount:false});return;}selectedAccount=saved.account_id;account=saved;writeUrl({account_id:selectedAccount});await refresh();}catch(error){if(ticket===accountTicket)localError('account',error);}},{signal});
  host.querySelector('[data-save-snapshot]').addEventListener('click',async event=>{const ticket=accountTicket;clearError('account');try{if(!account)throw new Error('先选择需要更新的账户。');if(!form.reportValidity())return;const id=account.account_id;await busy(event.currentTarget,()=>api.saveTradingSnapshot(id,accountSnapshot(form),signal),'保存新快照…');if(alive()&&ticket===accountTicket&&selectedAccount===id)await refresh();}catch(error){if(ticket===accountTicket)localError('account',error);}},{signal});
  host.querySelector('[data-load-template]').addEventListener('click',async event=>{const ticket=accountTicket;try{const value=await busy(event.currentTarget,()=>api.tradingTemplate(signal),'读取模板…');if(alive()&&ticket===accountTicket)form.querySelector('[name=snapshot_json]').value=JSON.stringify(value.snapshot||value,null,2);}catch(error){if(ticket===accountTicket)localError('account',error);}},{signal});
  host.querySelector('[data-import-snapshot]').addEventListener('change',async event=>{const ticket=accountTicket;try{const file=event.target.files[0];if(!file)return;if(file.size>2_000_000)throw new Error('账户JSON请控制在2 MB内。');const value=JSON.parse(await file.text());if(!alive()||ticket!==accountTicket)return;form.querySelector('[name=snapshot_json]').value=JSON.stringify(value.snapshot||value,null,2);if(value.name)form.querySelector('[name=name]').value=value.name;}catch(error){if(ticket===accountTicket)localError('account',error);}},{signal});
  planForm.addEventListener('submit',async event=>{
    event.preventDefault();if(submitting)return;submitting=true;clearError('plan');updateButton();
    try{
      const checked=await preflight();if(!checked||checked.status!=='ready'||!sameRequest(checked.request,currentRequest()))return;
      const request={...checked.request},token=++jobTicket;let finished=false;watcher?.stop();
      const target=host.querySelector('[data-plan-job]');target.hidden=false;
      const tracked=await submitJob(target,planButton,'trade_plan',{...request,source_sha256:checked.identity.source_sha256},{signal,onComplete:async job=>{finished=true;await completion(token,request)(job);}});
      if(!alive()||token!==jobTicket||!sameRequest(request,currentRequest())){tracked?.stop();return;}
      watcher=tracked;if(tracked&&!finished)writeUrl({plan_job:jobId(tracked.job),plan_id:''});
    }catch(error){if(alive())localError('plan',error);}finally{submitting=false;if(alive())updateButton();}
  },{signal});
  host.querySelector('[data-trading-plans]').addEventListener('click',async event=>{const button=event.target.closest('[data-open-plan]');if(button)await busy(button,()=>openPlan(button.dataset.openPlan),'读取中…');},{signal});
  host.querySelector('[data-fill-template]').addEventListener('click',()=>{if(!plan)return;host.querySelector('[name=fills_json]').value=JSON.stringify({fills:(plan.orders||[]).map(row=>({external_fill_id:'',order_id:row.order_id,date:plan.execution_date,quantity:0,price:0,fees:0}))},null,2);},{signal});
  host.querySelector('[data-fill-form]').addEventListener('submit',async event=>{event.preventDefault();clearError('fill');try{if(!plan)throw new Error('先读取一个计划。');if(!account||account.account_id!==plan.account_id)throw new Error('请选择此计划所属账户并核对最新快照。');let body;try{body=JSON.parse(formValue(event.currentTarget,'fills_json'));}catch{throw new Error('实际成交JSON格式不正确。');}if(!body.fills?.length)throw new Error('请填写至少一笔真实成交。');const id=account.account_id,result=await busy(event.currentTarget.querySelector('[type=submit]'),()=>api.recordTradingFills(plan.plan_id,{...body,expected_snapshot_id:account.latest_snapshot_id},signal),'核验实际成交…');if(!alive()||selectedAccount!==id)return;host.querySelector('[data-fill-result]').innerHTML=`<div class="notice">${badge(result.status)}<p>账户已按核验的实际成交更新；未成交计划数量没有自动入账。</p>${disclosure('成交与新快照',result)}</div>`;await refresh();}catch(error){localError('fill',error);}},{signal});
  sourceChanged();await refresh();
  if(alive()&&params.get('plan_id'))await openPlan(params.get('plan_id'),{restoreAccount:true});
  if(alive()&&params.get('plan_job'))await restoreJob(params.get('plan_job'));
  return()=>{disposed=true;++accountTicket;++refreshTicket;++previewTicket;++planTicket;++jobTicket;watcher?.stop();};
}



