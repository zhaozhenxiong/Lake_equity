import {api} from '../core/api.js';
import {arrayOf,badge,dateTime,empty,escapeHtml,heading,kindLabel,number,routeLink,showError,table} from '../core/ui.js';
import {createDatasetChart} from '../core/charts.js';
import {bindReportLinks,reportList} from '../core/reports.js';

export async function mount(host,{signal}) {
  host.innerHTML=heading('研究概览','从数据版本出发，连接结构、量价证据与可复核的模拟账本。',`<a class="button primary" href="#analysis">打开结构分析</a><a class="button" href="#data">浏览数据湖</a>`)+`<div data-overview-metrics class="metrics"><div class="metric"><div class="metric-label">行情记录</div><div class="metric-value">—</div></div><div class="metric"><div class="metric-label">股票覆盖</div><div class="metric-value">—</div></div><div class="metric"><div class="metric-label">完整覆盖日</div><div class="metric-value">—</div></div><div class="metric"><div class="metric-label">活动任务</div><div class="metric-value">—</div></div></div><div class="grid grid-two"><section class="panel"><div class="panel-header"><div><h2>数据湖覆盖</h2><p>数据集登记与实际填充状态，按层统计</p></div><a class="small" href="#data">查看目录</a></div><div class="chart-area hero-chart" data-dataset-chart>${empty('正在读取数据目录','覆盖图仅使用当前实际数据集状态。')}</div><div class="chart-caption" data-catalog-caption>正在核对本地可读范围</div></section><section class="panel"><div class="panel-header"><h2>当前研究边界</h2>${badge('local','本地')}</div><div class="panel-body" data-research-status><div class="loading-state">正在读取输入与引擎状态…</div></div></section></div><section class="panel space-top"><div class="quick-links"><a class="quick-link" href="#analysis"><strong>结构与量价</strong><p>分型、笔、中枢、威科夫与 ICT 来源证据</p></a><a class="quick-link" href="#backtest"><strong>独立方法回测</strong><p>实际执行、费用、拒单与期末持仓</p></a><a class="quick-link" href="#models"><strong>预测与模型</strong><p>Kronos 路径、均线特征和评分候选</p></a></div></section><div class="section-divider"><h2>最近保存的研究</h2><a class="small" href="#tasks?tab=reports">全部报告</a></div><section class="panel" data-recent-reports><div class="loading-state">正在读取报告索引…</div></section>`;
  let chart;
  bindReportLinks(host,signal);
  const results=await Promise.allSettled([api.overview(signal),api.catalog(signal),api.reports(signal)]);
  if(signal.aborted)return()=>{};
  const [overviewResult,catalogResult,reportsResult]=results;
  if(overviewResult.status==='fulfilled'){
    const overview=overviewResult.value,data=overview.data || {},jobs=overview.jobs || {},research=overview.research || {};
    host.querySelector('[data-overview-metrics]').innerHTML=`<div class="metric"><div class="metric-label">行情记录</div><div class="metric-value">${number(data.rows,0)}</div><div class="metric-detail">${escapeHtml(data.start || '—')} 至 ${escapeHtml(data.end || '—')}</div></div><div class="metric"><div class="metric-label">股票覆盖</div><div class="metric-value">${number(data.symbols,0)}<span class="small muted"> 只</span></div><div class="metric-detail">按实际原始库统计</div></div><div class="metric"><div class="metric-label">完整覆盖日</div><div class="metric-value" style="font-size:21px">${escapeHtml(data.covered_end || '待核验')}</div><div class="metric-detail">最新记录日不等于完整覆盖日</div></div><div class="metric"><div class="metric-label">活动任务</div><div class="metric-value">${number(jobs.active,0)}</div><div class="metric-detail">${jobs.total!==undefined?`本地任务记录 ${number(jobs.total,0)} 条`:'仅显式提交的计算'}</div></div>`;
    host.querySelector('[data-research-status]').innerHTML=`<dl class="detail-list"><div><dt>研究引擎</dt><dd>${badge(research.available===false?'unavailable':research.available===true?'available':'unknown',research.available===undefined?'待核验':undefined)}</dd></div><div><dt>行情输入</dt><dd>${badge(data.available===true?'available':data.available===false?'unavailable':'unknown',data.available===undefined?'待核验':undefined)}</dd></div><div><dt>价格口径</dt><dd>${escapeHtml(data.price_basis || research.price_basis || '以每次读取凭证为准')}</dd></div><div><dt>默认方法</dt><dd>结构与量价</dd></div><div><dt>计算方式</dt><dd>按需运行，保存后重开</dd></div></dl>${research.error?`<div class="notice error space-top">${escapeHtml(research.error)}</div>`:''}<p class="risk-note">来源参考 B/S、预测采样与实际模拟成交分别展示。训练完成与历史收益不构成前瞻资格。</p>`;
  }else{showError(host,overviewResult.reason);host.querySelector('[data-research-status]').innerHTML=empty('状态读取失败','检查服务连接，使用顶栏刷新重试。');}
  if(catalogResult.status==='fulfilled'){
    const datasets=arrayOf(catalogResult.value,'datasets');
    chart=createDatasetChart(host.querySelector('[data-dataset-chart]'),datasets);
    host.querySelector('[data-catalog-caption]').textContent=`登记 ${datasets.length} 个数据集；${datasets.filter(item=>item.has_data===true).length} 个已有本地数据。登记数量不代表完整历史覆盖。`;
  }else{host.querySelector('[data-dataset-chart]').innerHTML=empty('目录暂不可读',catalogResult.reason.message);}
  host.querySelector('[data-recent-reports]').innerHTML=reportsResult.status==='fulfilled'?reportList(arrayOf(reportsResult.value,'reports'),{limit:6}):empty('报告索引读取失败',reportsResult.reason.message);
  return()=>chart?.destroy();
}
