import {api} from './api.js';
import {arrayOf,badge,dataTable,dateTime,day,disclosure,downloadJson,empty,errorAdvice,errorHtml,escapeHtml,fullReportLink,kindLabel,number,percent,pretty,table,routeLink} from './ui.js';
import {createLineChart,createResearchChart} from './charts.js';
import {modelName} from './models.js';
import {replayReport} from './kronos-replay.js';
import {trainingRetentionHtml} from './retention-status.js';

export function isPipelinePreview(record){
  return record?.kind==='preview' || record?.params?.dry_run===true || record?.config?.dry_run===true;
}

export function reportStatusLabel(record){
  return record?.status_label || (isPipelinePreview(record)?'只读预检（历史记录）':undefined);
}

export function reportAccounts(result) {
  if(result?.portfolio)return [{key:result.mode==='rolling'?'滚动模型账户':'固定模型账户',...result.portfolio}];
  const values = result?.comparisons || result?.accounts;
  if (Array.isArray(values)) return values.map((value,index)=>({key:value.strategy || value.policy || String(index),...value}));
  if (values && typeof values === 'object') return Object.entries(values).map(([key,value])=>({key,...value}));
  return result?.daily || result?.trades || result?.metrics ? [{key:result.policy || result.strategy || '当前账户',...result}] : [];
}

export function reportId(report) { return report.run_id || report.result_id || report.id || ''; }

// 受控“生成次日计划”动作：第一次点击只显示内联确认，确认后才会请求后端。
export const PLAN_CONFIRM_TEXT='仅研究计划，不下单、不写账户；使用回测期末研究账户';
export const PLAN_ACCOUNT_SCOPE='backtest_terminal_research_account';
export const exclusionReasonLabel=value=>({rank_beyond_cap:'排名超过上限',liquidity_below_threshold:'成交额低于流动性门槛',insufficient_recent_coverage:'近期覆盖不足',insufficient_history_coverage:'历史覆盖不足',latest_bar_missing_or_invalid:'最新交易bar缺失或无效',insufficient_listing_history:'上市历史不足',outside_observed_listing_window:'超出已观测上市窗口',not_in_saved_evaluation_data:'不在已保存评估数据中',no_valid_bar_as_of:'信号日没有有效bar'})[value] || '资格未通过';

// cohort_effective：有效评分口径 + 逐只排除/不可评分披露；旧报告没有该字段时返回空字符串。
export function cohortEffectiveHtml(result){
  const cohort=result?.cohort_effective;
  if(!cohort || typeof cohort!=='object')return '';
  const excluded=arrayOf(cohort,'excluded'),unavailable=arrayOf(cohort,'unavailable');
  const policy=cohort.release_universe_policy && typeof cohort.release_universe_policy==='object'?cohort.release_universe_policy:{};
  const scope={release:'默认释放股票池',explicit_symbols:'显式股票池（关注池/指定股票）'}[cohort.universe_scope] || String(cohort.universe_scope || '股票池口径未返回');
  const cap=cohort.cap_applied===true?'已应用 1000 只上限':cohort.cap_applied===false?'未应用数量上限（显式股票池跳过排名截断）':'上限状态未返回';
  const excludedTable=excluded.length?table([{label:'被排除股票',render:row=>`<span class="symbol">${escapeHtml(row.symbol || '—')}</span>`},{label:'原因',render:row=>`${escapeHtml(exclusionReasonLabel(row.reason))} <span class="code-key">${escapeHtml(row.reason || '—')}</span>`},{label:'排名',render:row=>number(row.rank,0)},{label:'60日成交额中位数',render:row=>number(row.amount60_median)}],excluded):'';
  const unavailableTable=unavailable.length?table([{label:'不可评分股票',render:row=>`<span class="symbol">${escapeHtml(row.symbol || '—')}</span>`},{label:'原因',render:row=>`${escapeHtml(exclusionReasonLabel(row.reason))} <span class="code-key">${escapeHtml(row.reason || '—')}</span>`}],unavailable):'';
  return `<div class="cohort-effective" data-cohort-effective><p><strong>有效评分 ${number(cohort.scored,0)}/${number(cohort.requested,0)}</strong> · 股票池 ${escapeHtml(scope)} · ${escapeHtml(cap)}${policy.version?` · 口径 ${escapeHtml(policy.version)}${policy.max_members!==undefined?`（上限 ${number(policy.max_members,0)} 只）`:''}`:''}</p><p class="form-note">被排除与不可评分股票逐只披露；不补数据、不静默扩大股票池。</p>${excludedTable}${unavailableTable}</div>`;
}

// 只有已完成、非计划的 model_backtest_v1 报告才允许“生成次日计划”。
export function isCompletedModelBacktest(result){
  if(!result || typeof result!=='object')return false;
  if(result.family!=='model_backtest_v1' || result.kind==='bplan' || result.schema==='backtest_conditional_plan_v1')return false;
  return ['completed','succeeded','success'].includes(String(result.status || ''));
}

export function runsBacktestPlanRequest({runId,confirmed}={}){
  return confirmed===true && Boolean(String(runId || '').trim());
}

export function planConfirmHtml(runId){
  if(!runId)return '';
  return `<div class="notice" data-plan-confirm-panel><strong>${escapeHtml(PLAN_CONFIRM_TEXT)}</strong><p>确认后才会请求生成计划；计划只保存为研究报告，不连接券商，也不改写模拟账户账本。首次点击不会生成。</p><div class="button-row"><button class="small-button primary" type="button" data-plan-confirm="${escapeHtml(runId)}">确认生成计划</button><button class="small-button" type="button" data-plan-cancel>取消</button></div></div>`;
}

export function planActionHtml(result,{runId=''}={}){
  const id=runId || reportId(result);
  if(!id || !isCompletedModelBacktest(result))return '';
  return `<div class="panel-body" data-plan-action><div class="button-row"><button class="small-button" type="button" data-plan-generate="${escapeHtml(id)}">生成次日计划</button><span class="small muted">受控动作：先确认，再生成只读研究计划。</span></div><div data-plan-zone></div></div>`;
}

export function modelBacktestExtrasHtml(result,{runId=''}={}){
  if(!result || result.family!=='model_backtest_v1')return '';
  return `<div class="panel-body"><p>${({rolling:'已保存滚动序列',historical:'历史滚动协议重建',research_replay:'当前权重历史研究回放',frozen:'固定冻结模型'})[result.mode] || '模型策略'}：<span class="code-key">${escapeHtml(result.series_id || result.model_id || '未返回')}</span></p><p class="risk-note">费用后研究账户收益；严格历史与模拟配置资格以保存报告为准。</p>${cohortEffectiveHtml(result)}</div>${planActionHtml(result,{runId})}`;
}

// 报告/计划错误建议：复用 ui.js 的共享映射（由页面实现者维护），这里只做包装以便报告侧统一调用。
export function reportErrorAdvice(error){
  const text=String(error?.message || error || '');
  return typeof errorAdvice==='function'?errorAdvice(text):'';
}

export function planErrorHtml(error){
  const advice=reportErrorAdvice(error);
  return `${errorHtml(error)}${advice?`<div class="notice">${escapeHtml(advice)}</div>`:''}`;
}

const planConstraintText=value=>Array.isArray(value)?value:arrayOf(value,'constraints');
const planConstraintLabel=value=>planConstraintText(value).map(item=>typeof item==='string'?item:(item?.label || item?.code || item?.reason || '')).filter(Boolean).join('、');

// backtest_plan_v1：研究计划报告（信号日/执行日、研究账户声明、订单/持有、限制、回到原回测）。
export function backtestPlanHtml(plan,{openButton=false}={}){
  if(!plan || typeof plan!=='object')return '';
  const orders=arrayOf(plan,'orders'),holds=arrayOf(plan,'holds'),limitations=arrayOf(plan,'limitations').filter(item=>typeof item==='string' && item.trim());
  const missing=arrayOf(plan,'valuation_missing_symbols'),selected=arrayOf(plan,'selected');
  const account=plan.account && typeof plan.account==='object'?plan.account:{};
  const scope=plan.account_scope || PLAN_ACCOUNT_SCOPE;
  const source=plan.source_backtest_run_id || '';
  const planId=plan.plan_id || '';
  const ordersTable=orders.length?table([{label:'股票',render:row=>`<span class="symbol">${escapeHtml(row.symbol || '—')}</span>`},{label:'方向',render:row=>{const side=String(row.side || '').toUpperCase();return `<span class="${side==='BUY'?'up':side==='SELL'?'down':''}">${escapeHtml(side==='BUY'?'买入':side==='SELL'?'卖出':row.side || '—')}</span>`;}},{label:'数量',render:row=>number(row.quantity,0)},{label:'限价',render:row=>number(row.limit_price)},{label:'预计金额',render:row=>number(row.estimated_notional)},{label:'预计费用',render:row=>number(row.estimated_fees)},{label:'约束',wrap:true,render:row=>escapeHtml(planConstraintLabel(row.constraints) || '—')}],orders):empty('计划没有拟执行订单','计划可能只包含持有或参考价缺失的股票；没有订单不等于可以下单。');
  const holdsTable=holds.length?table([{label:'持有/跳过股票',render:row=>`<span class="symbol">${escapeHtml(row.symbol || '—')}</span>`},{label:'当前数量',render:row=>number(row.current_shares ?? row.quantity,0)},{label:'目标权重',render:row=>percent(row.target_weight)},{label:'原因',wrap:true,render:row=>escapeHtml([row.reason,planConstraintLabel(row.constraints)].filter(Boolean).join(' · ') || '未返回原因')}],holds):'';
  const buttons=[source?`<button class="small-button" type="button" data-plan-source-backtest="${escapeHtml(source)}">回到原回测报告</button>`:'',openButton&&planId?`<button class="small-button" type="button" data-plan-open="${escapeHtml(planId)}">查看计划报告</button>`:''].filter(Boolean).join('');
  return `<div class="panel-body" data-backtest-plan data-plan-id="${escapeHtml(planId)}"><h3>次日条件计划 · 研究计划</h3><div class="notice" data-plan-disclaimer><strong>仅研究计划，不下单、不写账户；使用回测期末研究账户。</strong><p>account_scope：<span class="code-key">${escapeHtml(scope)}</span>。不连接券商、不写交易账本；计划不等于成交，实际成交需人工核验。</p></div><dl class="detail-list"><div><dt>信号日 / 执行日</dt><dd>${escapeHtml(plan.signal_date || '—')} / ${escapeHtml(plan.execution_date || '—')} · ${escapeHtml(plan.next_session_source || '交易日来源未返回')}</dd></div><div><dt>研究账户</dt><dd>${escapeHtml(scope)} · 现金 ${number(account.cash)}</dd></div><div><dt>候选 / 订单 / 持有</dt><dd>${number(selected.length,0)} / ${number(orders.length,0)} / ${number(holds.length,0)}</dd></div><div><dt>来源回测</dt><dd class="code-key">${escapeHtml(source || '未返回')}</dd></div></dl>${ordersTable}${holds.length?'<h3>持有与跳过</h3>':''}${holdsTable}${missing.length?`<p class="form-note">参考价或估值缺失股票：${missing.map(item=>escapeHtml(item)).join('、')}；逐只列入持有，不补价格。</p>`:''}${limitations.length?`<div class="plan-limitations"><h3>限制与披露</h3><ul>${limitations.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul></div>`:''}${buttons?`<div class="button-row space-top">${buttons}</div>`:''}</div>`;
}

const planActionHosts=new WeakSet();
// 事件委托：生成按钮只显示确认；确认按钮通过 api.backtestPlan(runId, signal) 请求计划。
function bindPlanActions(content){
  if(!content || typeof content.addEventListener!=='function' || planActionHosts.has(content))return;
  planActionHosts.add(content);
  content.addEventListener('click',async event=>{
    const target=event.target;
    if(!target || typeof target.closest!=='function')return;
    const generate=target.closest('[data-plan-generate]');
    if(generate){const zone=content.querySelector('[data-plan-zone]');if(zone)zone.innerHTML=planConfirmHtml(generate.dataset.planGenerate);return;}
    const cancel=target.closest('[data-plan-cancel]');
    if(cancel){const zone=cancel.closest('[data-plan-zone]') || content.querySelector('[data-plan-zone]');if(zone)zone.innerHTML='';return;}
    const confirm=target.closest('[data-plan-confirm]');
    if(confirm){
      const runId=confirm.dataset.planConfirm || '',zone=content.querySelector('[data-plan-zone]');
      if(!runsBacktestPlanRequest({runId,confirmed:true}) || !zone)return;
      zone.innerHTML='<div class="loading-state">正在生成次日研究计划…</div>';
      try{
        const plan=typeof api.backtestPlan==='function'?await api.backtestPlan(runId):null;
        if(!plan || typeof plan!=='object')throw new Error('计划生成没有返回可读取的报告；请检查后端 /backtests/{run_id}/plan 是否已部署。');
        zone.innerHTML=backtestPlanHtml(plan,{openButton:true});
      }catch(error){zone.innerHTML=planErrorHtml(error);}
      return;
    }
    const sourceButton=target.closest('[data-plan-source-backtest]');
    if(sourceButton){openReport(sourceButton.dataset.planSourceBacktest);return;}
    const openButton=target.closest('[data-plan-open]');
    if(openButton){openReport(openButton.dataset.planOpen);return;}
  });
}


const policyLabel = value => ({structure_rules:'结构与量价',structure_kronos:'结构 + Kronos',structure_kronos_score:'结构 + Kronos + 评分',structure_kronos_l2:'同输入 L2 对照'})[value] || value;

export function reportList(reports, {limit=10}={}) {
  return table([
    {label:'研究运行',render:row=>`<button class="row-link" data-report-id="${escapeHtml(reportId(row))}">${escapeHtml(isPipelinePreview(row)?'流水线预检':row.title || kindLabel(row.kind || row.family))}</button><div class="muted small">${escapeHtml(reportId(row).slice(0,30))}</div>`},
    {label:'时间',render:row=>escapeHtml(dateTime(row.created_at))},
    {label:'状态',render:row=>badge(row.status || row.summary?.status || 'saved',reportStatusLabel(row) || (row.status || row.summary?.status ? undefined : '已保存'))},
    {label:'范围',render:row=>escapeHtml(row.symbol || row.summary?.symbols?.join('、') || row.summary?.data_range?.end || '—')},
    {label:'操作',render:row=>`<button class="small-button" data-report-id="${escapeHtml(reportId(row))}">读取报告</button>`}
  ],reports.slice(0,limit),{emptyTitle:'尚无保存的研究报告',emptyDescription:'完成分析、扫描或回测后，可在此重开原报告。读取不会重新运行计算。'});
}

export function bindReportLinks(host, signal) {
  host.addEventListener('click',event=>{
    const button=event.target.closest('[data-report-id]');
    if(button)openReport(button.dataset.reportId,signal);
  },{signal});
}

export async function openReport(id, signal) {
  const backdrop=document.createElement('div');
  backdrop.className='dialog-backdrop';
  backdrop.innerHTML=`<section class="dialog-panel" role="dialog" aria-modal="true" aria-labelledby="report-dialog-title"><div class="dialog-header"><h2 id="report-dialog-title">研究报告</h2><button class="icon-button" data-close aria-label="关闭报告">×</button></div><div class="dialog-body"><div class="loading-state">正在核验并读取保存报告…</div></div></section>`;
  document.body.append(backdrop);
  const previous=document.activeElement;
  let cleanup=()=>{};
  let closed=false;
  const controller=new AbortController();
  function close(){if(closed)return;closed=true;controller.abort();cleanup();backdrop.remove();document.removeEventListener('keydown',keyHandler);previous?.focus();}
  function keyHandler(event){if(event.key==='Escape')close();if(event.key==='Tab'){const buttons=[...backdrop.querySelectorAll('button,a[href],input,select,summary')].filter(item=>!item.disabled);const first=buttons[0],last=buttons.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}}
  backdrop.querySelector('[data-close]').addEventListener('click',close);
  backdrop.addEventListener('click',event=>{if(event.target===backdrop)close();});
  document.addEventListener('keydown',keyHandler);
  signal?.addEventListener('abort',close,{once:true});
  backdrop.querySelector('[data-close]').focus();
  try {
    const response=await api.report(id,controller.signal);
    if(controller.signal.aborted)return;
    cleanup=renderReport(backdrop.querySelector('.dialog-body'),response,{id});
  } catch(error) {if(error.name!=='AbortError')backdrop.querySelector('.dialog-body').innerHTML=errorHtml(error);}
}

export function renderReport(host,response,{id=''}={}) {
  const result=response?.result && !response?.frequencies ? response.result : response;
  if(!result || typeof result!=='object'){host.innerHTML=empty('报告没有可读取内容','检查报告标识及引用对象是否仍存在。');return()=>{};}
  const charts=[];
  const accounts=reportAccounts(result);
  const reportStart=result.start || result.spec?.start || result.data_range?.start || '';
  const reportEnd=result.end || result.spec?.end || result.data_end || '';
  host.innerHTML=`<section class="panel"><div class="report-heading"><div><h2>${escapeHtml(isPipelinePreview(result)?'流水线预检':result.family==='frozen_factor_refit_v1'?'最终重拟合报告':result.symbol || kindLabel(result.kind || (accounts.length?'backtest':'analysis')))}${result.symbol?' · 研究报告':''}</h2><p>${escapeHtml(result.start || result.data_range?.start || '')} ${result.end || result.data_end ? '至' : ''} ${escapeHtml(result.end || result.data_end || '')}</p><p class="inline-code">${escapeHtml(result.run_id || result.result_id || id)}</p></div><div class="report-actions">${badge(result.status || 'saved',reportStatusLabel(result) || (result.status?undefined:'已保存'))}<button class="small-button" data-export>${result.dashboard_view?'导出界面摘要 JSON':'导出 JSON'}</button>${fullReportLink(result)}</div></div><div data-report-content></div>${disclosure('数据、方法与运行身份',result.run_context || result.identity || {data_version:result.data_version,price_basis:result.price_basis,source_sha256:result.source_sha256,method:result.analysis_method,as_of:result.as_of})}${disclosure(result.dashboard_view?'界面摘要与产物校验':'原始保存结果',result)}</section>`;
  const content=host.querySelector('[data-report-content]');
  const range=host.querySelector('.report-heading p');if(range)range.textContent=`${reportStart}${reportStart&&reportEnd?' 至 ':''}${reportEnd}`;
  host.querySelector('[data-export]').addEventListener('click',()=>downloadJson(result,`lake-equity-${result.run_id || result.result_id || id || 'report'}${result.dashboard_view?'-dashboard-summary':''}.json`));
  if(result.family==='ten_year_model_study_v1'){
    content.innerHTML=`<div class="panel-body"><h3>十年研究与阶段模型</h3><p>开发段选优：${escapeHtml(result.selected_protocol?.model_type)} / ${number(result.selected_protocol?.train_sessions,0)}交易日。最新重拟合权重待前瞻。</p><a class="button" href="${routeLink('models')}">到模型中心查看候选与发布</a></div>${dataTable(result.candidates || [])}`;return()=>{};
  }
  if(result.family==='fusion_scan_v1'){
    content.innerHTML=`<div class="panel-body"><h3>多因子与Kronos复核</h3><p>初筛分母 ${number(result.coverage?.baseline_total,0)}；复核分母 ${number(result.coverage?.total,0)}；可用 ${number(result.coverage?.succeeded,0)}；不可用 ${number(result.coverage?.unusable,0)}；失败 ${number(result.coverage?.failed,0)}；延期 ${number(result.coverage?.deferred,0)}。</p><a class="button" href="${routeLink('scan',{report:result.run_id})}">打开完整选股结果</a><a class="button" href="${routeLink('watchlist',{tab:'trading',fusion_id:result.run_id})}">结合持仓构建计划</a></div>`;return()=>{};
  }
  if(result.family==='scan_explanation_attachment_v1'){
    content.innerHTML=`<div class="panel-body"><h3>模型评分解释附件</h3><p>已核验 ${number(result.explanation?.count,0)} 条原评分；贡献来自原冻结模型与输入，附件不覆盖原报告。</p><a class="button" href="${routeLink('scan',{report:result.source_run_id})}">打开原选股与指标依据</a></div>`;return()=>{};
  }
  if(result.family==='kronos_replay_v1'){
    content.innerHTML=replayReport(result)+`<div class="panel-body"><a class="button" href="${routeLink('models',{replay:result.run_id})}">打开循环实验</a></div>`;return()=>{};
  }
  if(result.family==='backtest_plan_v1' || result.schema==='backtest_conditional_plan_v1'){
    content.innerHTML=backtestPlanHtml(result);bindPlanActions(content);return()=>{};
  }
  if(result.plan_id){content.innerHTML=`<div class="panel-body"><h3>次日条件买卖计划</h3><p>${escapeHtml(result.execution_date)}；${number(result.orders?.length,0)}条拟交易。${result.is_fixture?'合成验收账户。':''}未连接券商，不等于成交。</p><a class="button" href="${routeLink('watchlist',{tab:'trading',plan_id:result.plan_id,account_id:result.account_id})}">查看计划和回填实际成交</a></div>${dataTable(result.orders || [])}`;return()=>{};}
  if(['cross_sectional_return_v1','frozen_factor_refit_v1'].includes(result.family)){
    content.innerHTML=`<div class="panel-body">${trainingRetentionHtml(result.retention)}<p>训练后端 ${escapeHtml(result.spec?.backend || '—')} · ${result.spec?.scope==='all'?'全市场冻结人口':`${number(result.spec?.symbols?.length,0)} 只显式股票`} · ${number(result.folds?.length,0)} 个滚动窗口</p><p class="risk-note">${result.family==='frozen_factor_refit_v1'?'最终重拟合推理版本，没有独立样本外成绩。':'研究候选；训练完成不自动取得严格历史或模拟配置资格。'}</p><a class="button" href="#models?run_id=${encodeURIComponent(result.run_id || '')}">在模型中心核验完整训练报告</a></div>${table([{label:'模型',render:row=>escapeHtml(modelName(row.name))},{label:'特征',render:row=>number(row.feature_count,0)},{label:'样本外 Rank IC',render:row=>number(row.metrics?.rank_ic,4)},{label:'RMSE',render:row=>number(row.metrics?.rmse,4)},{label:'费用后研究收益',render:row=>percent(row.portfolio?.metrics?.total_return)}],arrayOf(result,'models'))}`;
    return()=>{};
  }
  if(accounts.length){
    content.innerHTML=`<div class="filter-bar"><label class="field-inline">账户视角<select data-account>${accounts.map((account,index)=>`<option value="${index}">${escapeHtml(policyLabel(account.key))}</option>`).join('')}</select></label><span class="small muted">真实模拟账本 · 费用后结果 · 历史研究</span></div><div data-account-content></div>`;
    if(result.family==='model_backtest_v1'){content.insertAdjacentHTML('afterbegin',modelBacktestExtrasHtml(result,{runId:reportId(result)}));bindPlanActions(content);}
    let accountChart;
    function selectAccount(index){
      accountChart?.destroy();
      const account=accounts[index],metrics=account.metrics || {};
      const target=content.querySelector('[data-account-content]');
      const unavailable=metrics.performance_status==='unavailable';
      const sharpeDetail=metrics.sharpe_reason?'样本不足，未计算':['日收益均值 / 标准差 × √252（rf 0%）',metrics.psr!==null&&metrics.psr!==undefined?`PSR ${percent(metrics.psr)}`:''].filter(Boolean).join(' · ');
      target.innerHTML=`${unavailable?'<div class="notice error">执行数据不足，绩效不可用；账本保留用于审计，不把 0% 当作有效收益。</div>':''}<div class="metrics" style="border:0;border-radius:0;margin:0"><div class="metric"><div class="metric-label">累计收益</div><div class="metric-value ${Number(metrics.total_return)>=0?'up':'down'}">${percent(metrics.total_return)}</div><div class="metric-detail">账本实际返回</div></div><div class="metric"><div class="metric-label">最大回撤</div><div class="metric-value">${percent(metrics.max_drawdown)}</div><div class="metric-detail">已覆盖交易日</div></div><div class="metric"><div class="metric-label">成交笔数</div><div class="metric-value">${number(metrics.fills ?? account.trades?.length,0)}</div><div class="metric-detail">不含拒单</div></div><div class="metric"><div class="metric-label">总费用</div><div class="metric-value">${number(metrics.total_fees ?? metrics.fees)}</div><div class="metric-detail">元</div></div><div class="metric"><div class="metric-label">Sharpe</div><div class="metric-value">${metrics.sharpe===null||metrics.sharpe===undefined?'—':number(metrics.sharpe,3)}</div><div class="metric-detail">${sharpeDetail}</div></div></div><div class="chart-area compact" data-equity></div><div class="chart-caption">权益基于已保存账本。部分完成、待退出与期末持仓保留原状态；该结果不授予收益认证。</div><div class="panel-header"><h3>实际成交</h3><span class="coverage-count">${number(account.trades?.length,0)} 笔</span></div>${table([{key:'date',label:'日期',render:row=>escapeHtml(day(row.date || row.time))},{key:'symbol',label:'股票'},{label:'方向',render:row=>`<span class="${row.side==='BUY'?'up':'down'}">${escapeHtml(row.side || row.action || '—')}</span>`},{label:'成交价',render:row=>number(row.price)},{label:'数量',render:row=>number(row.shares ?? row.quantity,0)},{label:'费用',render:row=>number(row.fee)},{key:'reason',label:'原因',wrap:true}],arrayOf(account.trades).slice(0,200),{emptyTitle:'没有实际成交',emptyDescription:'无成交不代表任务失败。检查候选、执行限制、拒单和覆盖状态。'})}${disclosure('拒单、待退出与账户详情',{status:account.status,orders:account.orders,holdings:account.holdings || account.terminal_state?.positions || account.state?.positions,metrics,coverage:result.population || result.prediction_coverage,lineage:account.lineage,model_id:result.model_id,series_id:result.series_id,qualification:result.qualification,limitations:account.limitations})}`;
      try {accountChart=createLineChart(target.querySelector('[data-equity]'),account.daily || account.equity_curve || [],{valueKey:account.daily?.[0]?.equity !== undefined ? 'equity' : account.equity_curve?.[0]?.equity !== undefined ? 'equity' : 'nav'});}catch(error){target.querySelector('[data-equity]').innerHTML=errorHtml(error);}
    }
    content.querySelector('[data-account]').addEventListener('change',event=>selectAccount(Number(event.target.value)));
    selectAccount(0);
    return()=>accountChart?.destroy();
  }
  if(result.frequencies){
    content.innerHTML=`<div class="chart-area" data-saved-chart></div><div class="chart-caption">${escapeHtml(result.symbol)} · 日线 · 数据截至 ${escapeHtml(result.data_end || result.as_of)} · 来源参考点不等于模拟成交</div>`;
    try{const chart=createResearchChart(content.querySelector('[data-saved-chart]'));chart.render(result);charts.push(chart);}catch(error){content.innerHTML=errorHtml(error);}
  }else if(Array.isArray(result.rows)){
    let page=1;const pageSize=100;
    function drawRows(){const pages=Math.max(1,Math.ceil(result.rows.length/pageSize));content.innerHTML=(result.population?`<div class="panel-body"><p>模型 ${escapeHtml(result.model_id || '—')} · ${escapeHtml(result.as_of || result.end || '')}</p><dl class="detail-list">${Object.entries(result.population).filter(([key])=>['total','scored','missing','failed','unprocessed'].includes(key)).map(([key,value])=>`<div><dt>${escapeHtml({total:'冻结总人口',scored:'已评分',missing:'输入缺失',failed:'推理失败',unprocessed:'未处理'}[key])}</dt><dd>${number(value,0)}</dd></div>`).join('')}</dl></div>`:'')+dataTable(result.rows.slice((page-1)*pageSize,page*pageSize),pageSize)+`<div class="table-footer"><button class="small-button" data-report-prev${page<=1?' disabled':''}>上一页</button><span>第 ${page}/${pages} 页 · 完整返回 ${result.rows.length} 行</span><button class="small-button" data-report-next${page>=pages?' disabled':''}>下一页</button></div>`;content.querySelector('[data-report-prev]').addEventListener('click',()=>{page--;drawRows();});content.querySelector('[data-report-next]').addEventListener('click',()=>{page++;drawRows();});}drawRows();
  }else{
    content.innerHTML=`<div class="panel-body"><p class="small muted">该报告按原格式保存。展开下方内容查看完整参数、质量、预测或模型证据。</p>${result.error?`<div class="notice error">${escapeHtml(result.error)}</div>`:''}</div>`;
  }
  return()=>charts.forEach(chart=>chart.destroy());
}
