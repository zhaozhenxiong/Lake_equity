import {badge,dateTime,empty,escapeHtml,number,table} from './ui.js';
import {decisionView,featureName,selectionKronos,selectionLinks,selectionScore,selectionScope,selectionStatus,reasonText} from './selection.js';

const statusNames={scored:'已评分',missing:'输入缺失',excluded:'动态池排除',failed:'推理失败',unprocessed:'尚未处理',not_in_baseline:'未纳入初筛'};
const reasonNames={rank_beyond_cap:'排名超过冻结池上限',liquidity_below_threshold:'成交额低于流动性门槛',insufficient_verified_history:'可核验历史不足',insufficient_recent_coverage:'近期覆盖不足',insufficient_history_coverage:'历史覆盖不足',latest_bar_missing_or_invalid:'最新行情缺失或无效',insufficient_listing_history:'上市历史不足',outside_observed_listing_window:'超出已观测上市窗口',ranking_incomplete:'排名尚未完整',release_policy_missing:'原报告未绑定发布策略规则',snapshot_not_latest:'账户快照已不是最新版本',account_signal_date_mismatch:'账户快照与信号日期不一致',holdings_not_reviewed:'部分持仓未纳入本次复核',holding_valuation_missing:'缺少同日持仓估值',rebalance_calendar_missing:'调仓交易日历未核验',input_not_scored:'输入未得到有效评分',quote_unusable:'参考价格不可用'};
const exact=value=>value!==null && value!==undefined && value!=='' && Number.isFinite(Number(value))?String(value):'—';
const signed=value=>Number(value)>0?`+${exact(value)}`:exact(value);
const metricValue=(value,withSign=false)=>exact(value)==='—'?'—':`<span title="完整值：${escapeHtml(exact(value))}">${withSign && Number(value)>0?'+':''}${escapeHtml(String(Number(Number(value).toPrecision(8))))}</span>`;
const rank=row=>row.factor_rank ?? row.rank;
const stock=row=>`<strong>${escapeHtml(row.name || '名称缺失')}</strong><span class="symbol">${escapeHtml(row.symbol)}</span>`;
const score=row=>`<span class="selection-score" title="原始完整分数：${escapeHtml(exact(selectionScore(row)))}">${number(selectionScore(row),8)}</span>`;
const advice=row=>{const value=decisionView(row);return `<span class="selection-advice" data-action="${escapeHtml(value.action)}">${escapeHtml(value.label)}</span>`;};
function reasons(row){
  const summary=row.explanation_summary,items=[];
  for(const [key,label] of [['positive','正向'],['negative','负向']]){const item=summary?.[key]?.[0];if(item)items.push(`${label}：${featureName(item)} ${signed(item.contribution)}`);}
  items.push(...decisionView(row).reasons);
  if(row.reason)items.push(reasonNames[row.reason] || row.reason);
  return [...new Set(items)].slice(0,2);
}
const reasonHtml=row=>{const values=reasons(row);return values.length?values.map(value=>`<span>${escapeHtml(value)}</span>`).join(''):'<span class="muted">原报告未保存个股指标解释</span>';};
const evidenceButton=row=>`<button type="button" class="small-button" data-selection-evidence="${escapeHtml(row.symbol)}">查看依据</button>`;

export function selectionRowsHtml(report,rows){
  if(!rows.length)return empty('没有匹配的结果','筛选只改变展示；缺失和排除不等于低分。');
  const fusion=report.family==='fusion_scan_v1';
  const desktop=table([
    {label:'因子排名',render:row=>number(rank(row),0)},
    ...(fusion?[{label:'策略排名',render:row=>number(row.fused_rank,0)}]:[]),
    {label:'股票',render:row=>`<div class="selection-stock">${stock(row)}${row.decision?.is_holding || row.is_holding?'<small>现有持仓</small>':''}</div>`},
    {label:'研究建议',render:advice},
    {label:'模型原始分数',render:score},
    {label:'关键依据',wrap:true,render:row=>`<div class="selection-reasons">${reasonHtml(row)}</div>`},
    {label:'数据 / Kronos',render:row=>`${badge(selectionStatus(row),statusNames[selectionStatus(row)] || selectionStatus(row))}<small class="selection-kronos">Kronos · ${escapeHtml(selectionKronos(row))}</small>`},
    {label:'证据',render:evidenceButton}
  ],rows,{className:'selection-table'});
  const cards=rows.map(row=>`<article class="selection-card"><div class="selection-card-head"><div class="selection-stock">${stock(row)}</div><span>因子 #${number(rank(row),0)}${fusion?`<br>策略 #${number(row.fused_rank,0)}`:''}</span></div><div class="selection-card-score">${advice(row)}<span>原始分数 ${score(row)}</span></div><div class="selection-reasons">${reasonHtml(row)}</div><div class="selection-card-foot"><span>${escapeHtml(statusNames[selectionStatus(row)] || selectionStatus(row))}<br>Kronos · ${escapeHtml(selectionKronos(row))}</span>${evidenceButton(row)}</div></article>`).join('');
  return `<div class="selection-desktop">${desktop}</div><div class="selection-cards">${cards}</div>`;
}

export function selectionOverviewHtml(report,decisions){
  const fusion=report.family==='fusion_scan_v1',population=report.population || {},coverage=report.coverage || {},temporal=decisions?.temporal,policy=decisions?.policy || {},qualification={...report.qualification,...decisions?.qualification?.original,...decisions?.qualification};
  const counts=fusion?[['baseline_total','初筛总人口'],['total','复核股票'],['succeeded','预测可用'],['unusable','预测不可用'],['failed','失败'],['deferred','预算延期']].map(([key,label])=>[label,coverage[key],null]):[['total','冻结总人口'],['scored','已评分'],['excluded','动态池排除'],['missing','输入缺失'],['failed','推理失败'],['unprocessed','尚未处理']].map(([key,label])=>[label,population[key],key==='total'?'':key]);
  const metrics=counts.map(([label,value,status])=>{const tag=status===null?'div':'button';return `<${tag}${tag==='button'?' type="button"':''} class="selection-metric"${status!==null?` data-selection-status="${status}"`:''}><span>${label}</span><strong>${number(value,0)}</strong></${tag}>`;}).join('');
  const display=policy.display_top_n ?? report.top?.length,topK=policy.decision_top_k ?? report.release_identity?.risk?.top_k ?? report.risk?.top_k;
  const suggestions={};for(const row of decisions?.rows || []){const value=decisionView(row);suggestions[value.action]??={label:value.label,count:0};suggestions[value.action].count++;}
  return `<div class="selection-result-context"><div><strong>数据截至 ${escapeHtml(report.as_of || report.end || '未返回')}</strong><span>生成 ${escapeHtml(dateTime(report.created_at))} · ${escapeHtml(selectionScope(report))}</span><span>原执行日 ${escapeHtml(temporal?.execution_date || report.next_session || '未返回')} · 可用时间 ${escapeHtml(dateTime(temporal?.available_at))}</span></div><div class="selection-temporal" data-selection-temporal="${escapeHtml(temporal?.status || 'unknown')}"><strong>${escapeHtml(temporal?.label || '时效待核验')}</strong><span>${escapeHtml(temporal?.reason || '只读历史结果；当前可执行资格尚未核验')}</span><span>当前计划资格：${temporal?.actionable===true?'仍须账户与风险预检':'不可直接执行'}</span></div></div>
    <div class="selection-metrics">${metrics}</div><div class="selection-policy"><span>报告任务：${({completed:'已完成',partial:'部分完成',failed:'失败',cancelled:'已取消'})[report.status] || escapeHtml(report.status || '未返回')}</span><span>展示前 ${number(display,0)} / 策略 Top-${number(topK,0)}（两个独立口径）</span><span>${qualification.research_only===true?'仅供研究 · ':''}独立前瞻：${escapeHtml(({pending:'待验证',passed:'已通过'})[qualification.independent_forward_status] || qualification.independent_forward_status || '未返回')}</span><span>模拟配置资格：${qualification.paper_allocation_eligible===true?'已授予':'未授予'}</span></div>
    ${Object.keys(suggestions).length?`<div class="selection-suggestions">${Object.entries(suggestions).map(([action,value])=>`<button type="button" data-selection-suggestion="${escapeHtml(action)}">${escapeHtml(value.label)} <strong>${number(value.count,0)}</strong></button>`).join('')}</div>`:''}
    <p class="selection-note">完整分母保留，排序与筛选不改变冻结排名。原始分数、池内名次和研究建议均不是上涨概率或交易指令。${fusion?'Kronos采样比例未校准；因子与策略排名分别保留。':'Kronos本次未执行。'}</p>
    ${fusion && coverage.deferred?`<div class="selection-policy"><button type="button" class="small-button" data-resume-fusion="${escapeHtml(report.run_id)}">按原版本继续未完成预测</button></div>`:''}`;
}

export function selectionEvidenceHtml(report,row){
  const decision=decisionView(row),links=selectionLinks(report,row),blockers=(row.decision?.blockers || []).map(reasonText).map(value=>reasonNames[value] || value);
  return `<div class="selection-drawer-heading"><div class="selection-stock">${stock(row)}</div><button type="button" class="small-button" data-selection-close aria-label="关闭个股依据">关闭</button></div><div class="selection-evidence-summary">${advice(row)}<span>因子排名 ${number(rank(row),0)}${report.family==='fusion_scan_v1'?` / 策略排名 ${number(row.fused_rank,0)}`:''}</span><span>原始完整分数 <code>${escapeHtml(exact(selectionScore(row)))}</code></span></div><p>${escapeHtml(decision.reasons.join('；') || '原报告没有保存研究决策解释，不从正负分推断买卖。')}</p>${blockers.length?`<div class="notice">资格限制：${escapeHtml(blockers.join('；'))}</div>`:''}<p class="selection-note">数据：${escapeHtml(statusNames[selectionStatus(row)] || selectionStatus(row))} · Kronos：${escapeHtml(selectionKronos(row))} · 特征填补 ${number(row.imputed_feature_count,0)} 项</p><dl class="selection-source"><div><dt>信号日</dt><dd>${escapeHtml(report.as_of || report.end || '—')}</dd></div><div><dt>报告 / 模型 / 发布</dt><dd>${escapeHtml(report.run_id)}<br>${escapeHtml(report.model_id)}<br>${escapeHtml(report.release_id || '独立冻结模型')}</dd></div><div><dt>数据代际 read_ref</dt><dd>${escapeHtml(report.read_ref || '未返回')}</dd></div></dl><div class="selection-actions"><a class="button" href="${escapeHtml(links.analysis)}">同日结构读取</a>${links.backtest?`<a class="button" href="${escapeHtml(links.backtest)}">本报告冻结版本回测</a>`:''}<button type="button" data-selection-addwatch="${escapeHtml(row.symbol)}">加入关注</button></div><p class="selection-note">结构入口按相同股票与日期重新读取；未验证为原扫描的全部冻结输入。</p><h3>指标贡献</h3><div data-selection-explanation><div class="loading-state">读取原报告已保存解释…</div></div>`;
}

export function explanationHtml(value){
  if(value?.status==='not_saved' && value.reason==='row_was_not_scored')return '<div class="notice">本行未评分，没有可解释的模型贡献。先核对输入缺失或动态池排除原因，不为缺失数据伪造指标解释。</div>';
  if(value?.status==='not_saved')return '<div class="notice">原报告未保存指标解释。可显式生成独立附件，核对原代际和分数；不会覆盖原报告。</div><button type="button" data-selection-build-explanation>核验并生成本报告解释附件</button><div data-selection-explanation-job hidden></div>';
  if(value?.status==='unsupported')return '<div class="notice">该模型暂不提供逐指标贡献；仅保留实际规则证据，不套用 Ridge 解释。</div>';
  if(value?.status!=='saved')return '<div class="notice">没有经过核验的已保存指标解释，保留原始分数供查看。</div>';
  const features=value?.features || [];
  const columns=[{label:'指标 / 组',render:row=>`${escapeHtml(featureName(row))}<small class="selection-kronos">${escapeHtml(row.name)} / ${escapeHtml(row.group || '—')}</small>`},{label:'原值 / 填补',render:row=>`${metricValue(row.raw_value)}<br>${row.imputed?'已按训练中位数填补':'未填补'}`},{label:'裁剪值',render:row=>metricValue(row.clipped_value)},{label:'标准化值',render:row=>metricValue(row.standardized_value)},{label:'系数',render:row=>metricValue(row.coefficient,true)},{label:'贡献',render:row=>`<strong>${metricValue(row.contribution,true)}</strong>`}];
  const mobile=features.map(row=>`<details class="selection-feature"><summary>${escapeHtml(featureName(row))}<strong>${escapeHtml(signed(row.contribution))}</strong></summary><dl class="selection-source">${[['指标 / 组',`${row.name} / ${row.group || '—'}`],['原始值',exact(row.raw_value)],['填补',row.imputed?`训练中位数 ${exact(row.filled_value)}`:'未填补'],['裁剪值',exact(row.clipped_value)],['标准化值',exact(row.standardized_value)],['系数',signed(row.coefficient)]].map(([label,text])=>`<div><dt>${label}</dt><dd>${escapeHtml(text)}</dd></div>`).join('')}</dl></details>`).join('');
  return `${value.source?.base_scan_id?`<p class="selection-note">因子贡献来自原冻结扫描 <code>${escapeHtml(value.source.base_scan_id)}</code>；融合策略名次与筛选规则另行保留。</p>`:''}<div class="selection-explanation-totals"><span>原始分数 <code>${escapeHtml(exact(value.score))}</code></span><span>截距 <code>${escapeHtml(exact(value.intercept))}</code></span><span>截距 + 贡献之和 <code>${escapeHtml(exact(value.reconstructed_score))}</code></span><span>重算误差 <code>${escapeHtml(exact(value.reconstruction_error))}</code></span></div><p class="selection-note">真实模型贡献 = 系数 × 裁剪后标准化值；正负贡献不等于买卖指令。未训练的量价、结构和基本面指标未用于本次评分。</p><div class="selection-explanation-table">${table(columns,features)}</div><div class="selection-feature-cards">${mobile}</div>`;
}
