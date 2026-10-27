import {api} from '../core/api.js';
import {arrayOf,busy,coverageTiers,dateTime,empty,escapeHtml,heading,notify,number,routeLink,showError,statusLabel} from '../core/ui.js';
import {defaultMethodIdentity} from '../core/models.js';
import {submitJob} from '../core/jobs.js';
import {bindReportLinks,reportList} from '../core/reports.js';

const STAGES=[
  {id:'s0',title:'数据更新',hint:'覆盖全部数据集'},
  {id:'s1',title:'覆盖核验',hint:'A/B/C/D 结论'},
  {id:'s2',title:'默认方法',hint:'按冻结协议重拟合'},
  {id:'s3',title:'全市场选股',hint:'冻结人口'},
  {id:'s4',title:'默认回测',hint:'费用后账本'},
  {id:'s5',title:'资格评估',hint:'研究 / 模拟资格'},
  {id:'s6',title:'保存报告',hint:'可复核产物'},
  {id:'s7',title:'今日结果',hint:'报告与任务'}
];

const TERMINAL_SUCCESS=new Set(['complete','completed','succeeded','success']);
const TERMINAL_FAILURE=new Set(['failed','blocked']);
const ACTIVE_STATUS=new Set(['running','queued','pending','cancelling']);

function statusChip(value, label){
  const tone=TERMINAL_SUCCESS.has(value)?'good':value==='partial'?'warn':TERMINAL_FAILURE.has(value)?'bad':ACTIVE_STATUS.has(value)?'teal':'neutral';
  const text=label || ({complete:'已完成',completed:'已完成',succeeded:'已完成',success:'已完成',partial:'部分完成',failed:'失败',blocked:'已阻止',running:'运行中',queued:'排队中',pending:'待处理',cancelling:'取消中'})[value] || value || '尚未运行';
  return `<span class="status-chip ${tone}"><i aria-hidden="true"></i>${escapeHtml(text)}</span>`;
}

function stageStateLabel(state){return ({complete:'完成',partial:'部分',blocked:'阻断',running:'进行中',skip:'未启用'})[state] || '待运行';}

function renderMethod(host, method){
  const target=host.querySelector('[data-method-card]');
  target.innerHTML=`<div class="hero-method-inner">${defaultMethodIdentity(method, null, {showLink:true})}${method?'<p class="hero-method-note">默认只用这一版方法；其余模型、因子组与历史实验收进模型中心与高级区。</p>':''}</div>`;
}

function renderMetrics(host, {overview, summary, coverage, latest}){
  const data=overview?.data || {}, jobs=overview?.jobs || {};
  const target=host.querySelector('[data-overview-metrics]');
  const targetDate=summary?.target_date || coverage?.target_date || latest?.target_date || '';
  const verdict=coverage?.verdict || null;
  const scored=summary?.scored ?? summary?.scan?.scored ?? latest?.scored ?? latest?.scan?.scored ?? null;
  const counts=coverage?.counts || {};
  target.innerHTML=`<div class="metric"><div class="metric-label">目标日</div><div class="metric-value metric-date" data-metric-target>${escapeHtml(targetDate || '—')}</div><div class="metric-detail">${summary?.finished_at?`最近流水线完成于 ${escapeHtml(dateTime(summary.finished_at))}`:'按最近交易日确定目标日'}</div></div><div class="metric"><div class="metric-label">覆盖结论</div><div class="metric-value metric-chip">${verdict?statusChip(verdict):'<span class="status-chip neutral"><i aria-hidden="true"></i>尚未核验</span>'}</div><div class="metric-detail">${counts.registered!==undefined?`登记 ${number(counts.registered,0)} 个数据集 · 已尝试 ${number(counts.attempted,0)} 个`:`行情记录 ${number(data.rows,0)} 条 · 股票 ${number(data.symbols,0)} 只`}</div></div><div class="metric"><div class="metric-label">已评分</div><div class="metric-value">${scored===null?'—':number(scored,0)}</div><div class="metric-detail">${scored===null?'最近流水线未返回评分人数':'最近一次全市场选股已评分'}</div></div><div class="metric"><div class="metric-label">活动任务</div><div class="metric-value">${number(jobs.active,0)}</div><div class="metric-detail">${jobs.total!==undefined?`本地任务记录 ${number(jobs.total,0)} 条`:'仅显式提交的计算'}</div></div>`;
}

function renderPipelineSummary(host, summary, latest){
  const target=host.querySelector('[data-pipeline-state]');
  if(!summary && !latest){
    target.innerHTML='<span class="status-chip neutral"><i aria-hidden="true"></i>尚未运行</span><p>还没有流水线记录。运行首次研究后，这里显示最近一次结果。</p>';
    host.querySelector('[data-pipeline-badge]').innerHTML=statusChip(null,'尚未运行');
    return;
  }
  const status=summary?.status || latest?.status || '';
  const targetDate=summary?.target_date || latest?.target_date || '';
  target.innerHTML=`<div class="pipeline-latest-line">${statusChip(status)}<span class="small muted">目标日 ${escapeHtml(targetDate || '—')}</span></div><p class="small muted">${summary?.finished_at||latest?.finished_at?`完成于 ${escapeHtml(dateTime(summary?.finished_at || latest?.finished_at))}`:'仍在队列或运行中'}${summary?.exit_code!==undefined && summary?.exit_code!==null?` · 退出码 ${number(summary.exit_code,0)}`:''}</p><a class="small" href="${escapeHtml(routeLink('tasks',{job:summary?.pipeline_id || latest?.pipeline_id}))}">查看流水线详情</a>`;
  const badgeTarget=host.querySelector('[data-pipeline-badge]');
  if(badgeTarget)badgeTarget.innerHTML=statusChip(status);
}

function renderTimeline(host, {latest, preview}){
  const explicit=Array.isArray(latest?.stages)?latest.stages:Array.isArray(latest?.steps)?latest.steps:null;
  const plan=explicit && explicit.length ? explicit.slice(0,8) : Array.isArray(preview?.plan) && preview.plan.length ? preview.plan.slice(0,8) : STAGES;
  const status=latest?.status || '';
  const running=ACTIVE_STATUS.has(status), success=TERMINAL_SUCCESS.has(status);
  const normalize=value=>{const v=String(value || '').toLowerCase();if(['complete','completed','succeeded','success','done'].includes(v))return 'complete';if(v==='partial')return 'partial';if(['failed','blocked'].includes(v))return 'blocked';if(['running','active'].includes(v))return 'running';if(['skipped','skip','not_scheduled','not_enabled'].includes(v))return 'skip';return 'pending';};
  const rows=plan.map((stage,index)=>{
    const willRun=stage.will_run;
    let state='pending';
    if(explicit)state=normalize(stage.status || stage.state);
    else if(willRun===false)state='skip';
    else if(success)state='complete';
    else if(status==='partial')state='partial';
    else if(TERMINAL_FAILURE.has(status))state='blocked';
    else if(running)state=index===0?'running':'pending';
    return {title:stage.title || STAGES[index]?.title || `阶段 ${index}`, state, index};
  });
  host.querySelector('[data-pipeline-timeline]').innerHTML=rows.map((row,index)=>`<li class="pipeline-step" data-state="${row.state}"><span class="pipeline-index" aria-hidden="true">S${index}</span><span class="pipeline-dot" aria-hidden="true"></span><strong>${escapeHtml(row.title)}</strong><small>${escapeHtml(stageStateLabel(row.state))}</small></li>`).join('');
  const caption=host.querySelector('[data-pipeline-caption]');
  if(caption)caption.textContent=status?`最近流水线状态：${statusLabel(status)}；阶段状态来自整体状态与最近预检计划，逐阶段明细见任务详情。`:'尚未运行流水线。阶段列表是默认研究顺序；点“一键更新并生成今日结果”开始。';
}

function renderPreview(host, preview){
  const target=host.querySelector('[data-preview-result]');
  if(!preview){target.replaceChildren();return;}
  const blockers=arrayOf(preview,'blockers').map(item=>typeof item==='string'?item:(item.message || item.reason || JSON.stringify(item)));
  const plan=arrayOf(preview,'plan');
  const coverage=preview.coverage || null;
  const tiers=coverage?coverageTiers(coverage):[];
  const coverageLine=coverage?`<div class="preview-coverage">${coverage.verdict?statusChip(coverage.verdict):statusChip(null,'覆盖未返回')}<span class="small muted">目标日 ${escapeHtml(coverage.target_date || preview.target_date || '—')}</span>${tiers.map(tier=>`<span class="small muted">${tier.key} ${number(tier.value,0)} ${escapeHtml(tier.label)}</span>`).join('')}</div>`:'';
  target.innerHTML=`<div class="preview-result"><div class="preview-head">${preview.can_scan===false?statusChip('blocked','预检发现阻断'):statusChip('complete','预检通过')}<span class="small muted">目标日 ${escapeHtml(preview.target_date || '—')}</span>${preview.can_scan!==undefined?`<span class="status-chip ${preview.can_scan?'good':'bad'}"><i aria-hidden="true"></i>选股${preview.can_scan?'可运行':'被阻断'}</span>`:''}${preview.can_backtest!==undefined?`<span class="status-chip ${preview.can_backtest?'good':'bad'}"><i aria-hidden="true"></i>回测${preview.can_backtest?'可运行':'被阻断'}</span>`:''}</div>${coverageLine}<ul class="preview-plan">${plan.map(item=>`<li data-will-run="${item.will_run===false?'false':'true'}"><strong>${escapeHtml(item.title || item.id || '阶段')}</strong><span>${item.will_run===false?'本次跳过':'本次运行'}</span></li>`).join('')}</ul>${blockers.length?`<div class="notice error"><strong>阻断项</strong><ul>${blockers.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul></div>`:'<p class="form-note">预检未发现阻断项；预检不替代运行结果。</p>'}</div>`;
}

function renderCoverage(host, coverage){
  const target=host.querySelector('[data-coverage-strip]');
  if(!coverage){
    const tiers=coverageTiers(null).map(tier=>`<div class="coverage-tier" data-tone="${tier.tone}"><span class="coverage-key">${tier.key}</span><strong>—</strong><small>${escapeHtml(tier.label)}</small></div>`).join('');
    target.innerHTML=`<div class="coverage-head"><div><span class="coverage-eyebrow">数据覆盖</span><h2>覆盖结论尚未返回</h2><p>运行流水线或打开数据湖后查看 A/B/C/D 结论。缺失不会被当成通过。</p></div><span class="status-chip neutral"><i aria-hidden="true"></i>未核验</span></div><div class="coverage-tiers">${tiers}</div><div class="coverage-foot"><a class="small" href="#data">到数据湖查看逐数据集覆盖</a></div>`;
    return;
  }
  const counts=coverage.counts || {};
  target.innerHTML=`<div class="coverage-head"><div><span class="coverage-eyebrow">数据覆盖</span><h2>${escapeHtml(coverage.target_date || '最近核验')}</h2><p>${counts.registered!==undefined?`登记 ${number(counts.registered,0)} · 已尝试 ${number(counts.attempted,0)} · 未安排 ${number(Number(counts.not_scheduled || 0)+Number(counts.not_enabled || 0),0)}`:'登记数与尝试数以数据湖实际返回为准。'}</p></div>${coverage.verdict?statusChip(coverage.verdict):statusChip(null,'未核验')}</div><div class="coverage-tiers">${coverageTiers(coverage).map(tier=>`<div class="coverage-tier" data-tone="${tier.tone}"><span class="coverage-key">${tier.key}</span><strong>${number(tier.value,0)}</strong><small>${escapeHtml(tier.label)}</small></div>`).join('')}</div><div class="coverage-foot"><span class="small muted">A 完整 / B 部分 / C 未知或失败 / D 未安排。覆盖结论来自后端核验；缺口不会被补成通过。</span><a class="small" href="#data">逐数据集覆盖</a></div>`;
}

export async function mount(host,{signal}){
  host.innerHTML=heading('研究概览','一键更新数据覆盖、训练默认方法，并生成今日选股与回测。默认只保留一个方法。')+`<section class="hero-default"><div class="hero-method" data-method-card><div class="loading-state">正在读取默认方法…</div></div><div class="hero-actions"><p class="hero-eyebrow">今日研究 · 默认方法</p><button type="button" class="hero-primary" data-run-pipeline aria-label="一键更新数据覆盖、训练默认方法并生成今日选股与回测" title="更新数据覆盖 → 按冻结协议训练默认方法 → 全市场选股 → 默认回测 → 保存报告"><span class="hero-primary-title">一键更新并生成今日结果</span><span class="hero-primary-note">更新覆盖 → 训练默认方法 → 选股 → 回测</span></button><div class="hero-actions-row"><button type="button" class="small-button" data-preview-pipeline aria-label="预检今日流水线" title="读取覆盖、计划与阻断项；只读，不运行计算">预检</button><a class="small" href="#tasks">任务与报告</a><a class="small" href="#models">模型中心</a></div><div class="pipeline-latest" data-pipeline-state><div class="loading-state">正在读取最近流水线…</div></div><div class="job-inline" data-pipeline-job hidden></div></div></section><div class="metrics" data-overview-metrics><div class="metric"><div class="metric-label">目标日</div><div class="metric-value">—</div></div><div class="metric"><div class="metric-label">覆盖结论</div><div class="metric-value">—</div></div><div class="metric"><div class="metric-label">已评分</div><div class="metric-value">—</div></div><div class="metric"><div class="metric-label">活动任务</div><div class="metric-value">—</div></div></div><section class="panel pipeline-panel"><div class="panel-header"><div><h2>研究流水线</h2><p>一次运行覆盖 S0 → S7，产出保存在任务与报告中</p></div><span class="status-chip neutral" data-pipeline-badge><i aria-hidden="true"></i>读取中</span></div><ol class="pipeline-timeline" data-pipeline-timeline>${STAGES.map((stage,index)=>`<li class="pipeline-step" data-state="pending"><span class="pipeline-index" aria-hidden="true">S${index}</span><span class="pipeline-dot" aria-hidden="true"></span><strong>${stage.title}</strong><small>待运行</small></li>`).join('')}</ol><p class="pipeline-caption" data-pipeline-caption>正在读取最近流水线状态…</p><div data-preview-result></div></section><section class="coverage-strip" data-coverage-strip>${empty('正在核验数据覆盖','A/B/C/D 结论来自后端覆盖服务。')}</section><div class="section-divider"><h2>最近报告</h2><a class="small" href="#tasks?tab=reports">全部报告</a></div><section class="panel" data-recent-reports><div class="loading-state">正在读取报告索引…</div></section><div class="overview-links"><a class="small" href="#data">数据湖</a><a class="small" href="#analysis">结构与量价</a><a class="small" href="#models">模型中心</a><a class="small" href="#scan">模型选股</a><a class="small" href="#backtest">回测</a><a class="small" href="#tasks">任务</a></div>`;

  let watcher;
  let latestReport=null;
  let lastPreview=null;
  bindReportLinks(host,signal);

  async function load(){
    const [overviewResult,latestResult,reportsResult]=await Promise.allSettled([api.overview(signal),api.pipelineLatest(signal),api.reports(signal)]);
    if(signal.aborted)return;
    const overview=overviewResult.status==='fulfilled'?overviewResult.value:null;
    if(overviewResult.status==='rejected')showError(host,overviewResult.reason);
    if(latestResult.status==='fulfilled')latestReport=latestResult.value?.pipeline || null;
    else latestReport=null;
    let method=overview?.default_method;
    if(method===undefined){
      // Older backend: fall back to the active release so the page still identifies one method.
      try{const active=await api.activeRelease(signal);method=active?.release || null;}catch(error){method=null;}
      if(signal.aborted)return;
    }
    renderMethod(host,method || null);
    const summary=overview?.pipeline || null;
    const coverage=overview?.coverage || latestReport?.coverage || null;
    renderMetrics(host,{overview,summary,coverage,latest:latestReport});
    renderPipelineSummary(host,summary,latestReport);
    renderTimeline(host,{latest:latestReport || summary,preview:lastPreview});
    renderCoverage(host,coverage);
    host.querySelector('[data-recent-reports]').innerHTML=reportsResult.status==='fulfilled'?reportList(arrayOf(reportsResult.value,'reports'),{limit:5}):empty('报告索引读取失败',reportsResult.reason?.message || '检查服务连接后刷新。');
    if(latestResult.status==='rejected' && latestResult.reason?.status!==404)host.querySelector('[data-pipeline-state]').insertAdjacentHTML('beforeend',`<div class="notice error">最近流水线读取失败：${escapeHtml(latestResult.reason.message)}</div>`);
  }

  async function runPipeline(button){
    const target=host.querySelector('[data-pipeline-job]');
    target.hidden=false;
    watcher?.stop();
    try{
      watcher=await submitJob(target,button,'pipeline',{dry_run:false,packs:'full',refit:'auto'},{signal,onComplete:async job=>{
        if(signal.aborted)return;
        if(TERMINAL_FAILURE.has(job.result?.status))notify('流水线未完成，请到任务详情核对阻断项。','error');
        else notify('流水线已完成，正在刷新今日结果。');
        await load();
      }});
    }catch(error){if(error.name!=='AbortError')showError(host,error);}
  }

  async function preview(button){
    try{
      const value=await busy(button,()=>api.pipelinePreview({packs:'full',refit:'auto'},signal),'预检中…');
      if(signal.aborted)return;
      lastPreview=value;
      renderPreview(host,value);
      renderTimeline(host,{latest:latestReport,preview:value});
    }catch(error){
      if(error.name==='AbortError')return;
      const target=host.querySelector('[data-preview-result]');
      target.innerHTML=error.status===404?'<div class="notice"><strong>预检接口尚未就绪</strong><p>服务当前未提供流水线预检；可以直接运行一键流水线，运行结果会保留完整阻断信息。</p></div>':`<div class="notice error">${escapeHtml(error.message)}</div>`;
    }
  }

  host.querySelector('[data-run-pipeline]').addEventListener('click',event=>runPipeline(event.currentTarget),{signal});
  host.querySelector('[data-preview-pipeline]').addEventListener('click',event=>preview(event.currentTarget),{signal});
  renderTimeline(host,{latest:null,preview:null});
  await load();
  return()=>{watcher?.stop();};
}
