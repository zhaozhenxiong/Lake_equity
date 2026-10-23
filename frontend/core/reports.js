import {api} from './api.js';
import {arrayOf,badge,dataTable,dateTime,day,disclosure,downloadJson,empty,errorHtml,escapeHtml,kindLabel,number,percent,pretty,table} from './ui.js';
import {createLineChart,createResearchChart} from './charts.js';
import {modelName} from './models.js';

export function reportAccounts(result) {
  if(result?.portfolio)return [{key:result.mode==='rolling'?'滚动模型账户':'固定模型账户',...result.portfolio}];
  const values = result?.comparisons || result?.accounts;
  if (Array.isArray(values)) return values.map((value,index)=>({key:value.strategy || value.policy || String(index),...value}));
  if (values && typeof values === 'object') return Object.entries(values).map(([key,value])=>({key,...value}));
  return result?.daily || result?.trades || result?.metrics ? [{key:result.policy || result.strategy || '当前账户',...result}] : [];
}

export function reportId(report) { return report.run_id || report.result_id || report.id || ''; }

const policyLabel = value => ({structure_rules:'结构与量价',structure_kronos:'结构 + Kronos',structure_kronos_score:'结构 + Kronos + 评分',structure_kronos_l2:'同输入 L2 对照'})[value] || value;

export function reportList(reports, {limit=10}={}) {
  return table([
    {label:'研究运行',render:row=>`<button class="row-link" data-report-id="${escapeHtml(reportId(row))}">${escapeHtml(row.title || kindLabel(row.kind || row.family))}</button><div class="muted small">${escapeHtml(reportId(row).slice(0,30))}</div>`},
    {label:'时间',render:row=>escapeHtml(dateTime(row.created_at))},
    {label:'状态',render:row=>badge(row.status || row.summary?.status || 'saved',row.status || row.summary?.status ? undefined : '已保存')},
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
  host.innerHTML=`<section class="panel"><div class="report-heading"><div><h2>${escapeHtml(result.symbol || kindLabel(result.kind || (accounts.length?'backtest':'analysis')))}${result.symbol?' · 研究报告':''}</h2><p>${escapeHtml(result.start || result.data_range?.start || '')} ${result.end || result.data_end ? '至' : ''} ${escapeHtml(result.end || result.data_end || '')}</p><p class="inline-code">${escapeHtml(result.run_id || result.result_id || id)}</p></div><div class="report-actions">${badge(result.status || 'saved',result.status?undefined:'已保存')}<button class="small-button" data-export>导出 JSON</button></div></div><div data-report-content></div>${disclosure('数据、方法与运行身份',result.run_context || result.identity || {data_version:result.data_version,price_basis:result.price_basis,source_sha256:result.source_sha256,method:result.analysis_method,as_of:result.as_of})}${disclosure('原始保存结果',result)}</section>`;
  const content=host.querySelector('[data-report-content]');
  const range=host.querySelector('.report-heading p');if(range)range.textContent=`${reportStart}${reportStart&&reportEnd?' 至 ':''}${reportEnd}`;
  host.querySelector('[data-export]').addEventListener('click',()=>downloadJson(result,`lake-equity-${result.run_id || result.result_id || id || 'report'}.json`));
  if(['cross_sectional_return_v1','frozen_factor_refit_v1'].includes(result.family)){
    content.innerHTML=`<div class="panel-body"><p>训练后端 ${escapeHtml(result.spec?.backend || '—')} · ${result.spec?.scope==='all'?'全市场冻结人口':`${number(result.spec?.symbols?.length,0)} 只显式股票`} · ${number(result.folds?.length,0)} 个滚动窗口</p><p class="risk-note">${result.family==='frozen_factor_refit_v1'?'最终重拟合推理版本，没有独立样本外成绩。':'研究候选；训练完成不自动取得严格历史或模拟配置资格。'}</p><a class="button" href="#models?run_id=${encodeURIComponent(result.run_id || '')}">在模型中心核验完整训练报告</a></div>${table([{label:'模型',render:row=>escapeHtml(modelName(row.name))},{label:'特征',render:row=>number(row.feature_count,0)},{label:'样本外 Rank IC',render:row=>number(row.metrics?.rank_ic,4)},{label:'RMSE',render:row=>number(row.metrics?.rmse,4)},{label:'费用后研究收益',render:row=>percent(row.portfolio?.metrics?.total_return)}],arrayOf(result,'models'))}`;
    return()=>{};
  }
  if(accounts.length){
    content.innerHTML=`<div class="filter-bar"><label class="field-inline">账户视角<select data-account>${accounts.map((account,index)=>`<option value="${index}">${escapeHtml(policyLabel(account.key))}</option>`).join('')}</select></label><span class="small muted">真实模拟账本 · 费用后结果 · 历史研究</span></div><div data-account-content></div>`;
    if(result.family==='model_backtest_v1')content.insertAdjacentHTML('afterbegin',`<div class="panel-body"><p>${result.mode==='rolling'?'已保存滚动序列':'固定冻结模型'}：<span class="code-key">${escapeHtml(result.series_id || result.model_id || '未返回')}</span></p><p class="risk-note">费用后研究账户收益；严格历史与模拟配置资格以保存报告为准。</p></div>`);
    let accountChart;
    function selectAccount(index){
      accountChart?.destroy();
      const account=accounts[index],metrics=account.metrics || {};
      const target=content.querySelector('[data-account-content]');
      target.innerHTML=`<div class="metrics" style="border:0;border-radius:0;margin:0"><div class="metric"><div class="metric-label">累计收益</div><div class="metric-value ${Number(metrics.total_return)>=0?'up':'down'}">${percent(metrics.total_return)}</div><div class="metric-detail">账本实际返回</div></div><div class="metric"><div class="metric-label">最大回撤</div><div class="metric-value">${percent(metrics.max_drawdown)}</div><div class="metric-detail">已覆盖交易日</div></div><div class="metric"><div class="metric-label">成交笔数</div><div class="metric-value">${number(metrics.fills ?? account.trades?.length,0)}</div><div class="metric-detail">不含拒单</div></div><div class="metric"><div class="metric-label">总费用</div><div class="metric-value">${number(metrics.total_fees ?? metrics.fees)}</div><div class="metric-detail">元</div></div></div><div class="chart-area compact" data-equity></div><div class="chart-caption">权益基于已保存账本。部分完成、待退出与期末持仓保留原状态；该结果不授予收益认证。</div><div class="panel-header"><h3>实际成交</h3><span class="coverage-count">${number(account.trades?.length,0)} 笔</span></div>${table([{key:'date',label:'日期',render:row=>escapeHtml(day(row.date || row.time))},{key:'symbol',label:'股票'},{label:'方向',render:row=>`<span class="${row.side==='BUY'?'up':'down'}">${escapeHtml(row.side || row.action || '—')}</span>`},{label:'成交价',render:row=>number(row.price)},{label:'数量',render:row=>number(row.shares ?? row.quantity,0)},{label:'费用',render:row=>number(row.fee)},{key:'reason',label:'原因',wrap:true}],arrayOf(account.trades).slice(0,200),{emptyTitle:'没有实际成交',emptyDescription:'无成交不代表任务失败。检查候选、执行限制、拒单和覆盖状态。'})}${disclosure('拒单、待退出与账户详情',{status:account.status,orders:account.orders,holdings:account.holdings || account.terminal_state?.positions || account.state?.positions,metrics,coverage:result.population || result.prediction_coverage,lineage:account.lineage,model_id:result.model_id,series_id:result.series_id,qualification:result.qualification,limitations:account.limitations})}`;
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
