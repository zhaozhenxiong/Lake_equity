import {api} from '../core/api.js';
import {arrayOf,badge,busy,disclosure,empty,errorHtml,escapeHtml,formValue,heading,number,routeLink,showError,symbolsFrom,table,today} from '../core/ui.js';
import {submitJob} from '../core/jobs.js';
import {reportList} from '../core/reports.js';
import {bindModelSelector} from '../core/models.js';

export function scanPage(rows,{query='',status='',page=1,pageSize=100}={}){
  const term=query.toLowerCase(),filtered=rows.filter(row=>(!status||row.status===status)&&(!term||`${row.symbol} ${row.name || ''} ${row.reason || ''}`.toLowerCase().includes(term)));
  const pages=Math.max(1,Math.ceil(filtered.length/pageSize)),current=Math.min(Math.max(page,1),pages);
  return {rows:filtered.slice((current-1)*pageSize,current*pageSize),total:filtered.length,received:rows.length,pages,page:current};
}

export async function mount(host,{signal,params}){
  let watcher,result,page=1;
  host.innerHTML=heading('全市场模型选股','使用明确冻结的模型版本，按实际沪深 A 股人口分批推理；分开记录评分、缺失、失败与未处理。')+`<section class="panel"><div class="panel-header"><h2>选择模型与扫描范围</h2></div><form id="scan-form" class="form-toolbar"><div class="form-grid"><label class="span-two">冻结模型版本<select name="model_id" data-model-select required><option value="">正在读取模型版本…</option></select></label><label>研究范围<select name="scope"><option value="symbols">指定股票池</option><option value="all">全部沪深 A 股</option></select></label><label class="span-two">股票代码<input name="symbols" value="${escapeHtml(params.get('symbols') || '')}" placeholder="600519.SH,000001.SZ" required></label><label>行情截止<input name="end" type="date" value="${escapeHtml(params.get('end') || today())}" required></label><button type="submit" class="primary">使用该版本扫描</button></div><p class="form-note">全市场范围由后端冻结人口并分批处理，不以训练股票池代替全市场。分数不是上涨概率或直接买卖指令。</p><div data-model-identity></div></form><div class="job-inline" data-scan-job hidden></div></section><section class="panel space-top"><div class="panel-header"><div><h2>扫描结果</h2><p data-scan-caption>等待显式扫描或重开保存报告</p></div><div class="button-row"><input data-scan-filter aria-label="筛选扫描结果" placeholder="代码、名称、原因"><select data-scan-status aria-label="评分状态"><option value="">全部状态</option><option value="scored">已评分</option><option value="missing">输入缺失</option><option value="failed">推理失败</option><option value="unprocessed">尚未处理</option></select></div></div><div class="panel-body" data-scan-population></div><div data-scan-results>${empty('尚无模型扫描结果','先在模型中心训练，再明确选择一个冻结版本。')}</div><div class="table-footer"><button class="small-button" data-scan-previous disabled>上一页</button><span data-scan-pagination></span><button class="small-button" data-scan-next disabled>下一页</button></div><div data-scan-identity></div></section><div class="section-divider"><h2>保存的模型扫描</h2><button class="small-button" data-refresh-saved>刷新索引</button></div><section class="panel" data-saved-scans></section>`;
  const form=host.querySelector('#scan-form');
  function render(){
    const view=scanPage(arrayOf(result,'rows'),{query:host.querySelector('[data-scan-filter]').value,status:host.querySelector('[data-scan-status]').value,page});page=view.page;
    host.querySelector('[data-scan-results]').innerHTML=table([
      {key:'rank',label:'排名'},
      {label:'股票',render:row=>`<a class="symbol" href="${escapeHtml(routeLink('analysis',{symbol:row.symbol,end:result?.as_of || result?.end}))}">${escapeHtml(row.symbol)}</a><div class="small muted">${escapeHtml(row.name || '')}</div>`},
      {label:'模型分数',render:row=>number(row.score,5)},
      {label:'状态',render:row=>badge(row.status,({scored:'已评分',missing:'输入缺失',failed:'推理失败',unprocessed:'未处理'})[row.status])},
      {label:'训练人口',render:row=>row.is_training_member===true?'训练池内':row.is_training_member===false?'训练池外':'未返回'},
      {key:'reason',label:'原因',wrap:true},
      {label:'操作',render:row=>`<a href="${escapeHtml(routeLink('backtest',{model_id:result?.model_id,mode:'frozen',symbols:row.symbol,end:result?.as_of || result?.end}))}">配置该模型回测</a>`}
    ],view.rows,{emptyTitle:'没有匹配的评分行',emptyDescription:'检查状态筛选与覆盖，缺失不等同于低分。'});
    host.querySelector('[data-scan-pagination]').textContent=`第 ${page} / ${view.pages} 页 · 筛选 ${view.total} / 完整返回 ${view.received} 行`;
    host.querySelector('[data-scan-previous]').disabled=page<=1;host.querySelector('[data-scan-next]').disabled=page>=view.pages;
  }
  function showResult(value){result=value?.result || value;page=1;const population=result.population || {};host.querySelector('[data-scan-caption]').textContent=`${result.as_of || result.end || '—'} · 模型 ${result.model_id || '未返回'} · 报告 ${result.run_id || '—'}`;host.querySelector('[data-scan-population]').innerHTML=`<dl class="detail-list">${[['total','冻结总人口'],['scored','已评分'],['missing','缺失'],['failed','失败'],['unprocessed','未处理']].map(([key,label])=>`<div><dt>${label}</dt><dd>${number(population[key],0)}</dd></div>`).join('')}</dl>${badge(result.status)}<p class="small muted">人口分母来自完整后端报告，分页和筛选只改变展示。</p>`;host.querySelector('[data-scan-identity]').innerHTML=disclosure('模型版本、人口与覆盖身份',{model_id:result.model_id,model_version:result.model_version,population:result.population,qualification:result.qualification,sources:result.sources});render();}
  async function openReport(id){try{const report=await api.report(id,signal);if(!signal.aborted)showResult(report);}catch(error){showError(host,error);}}
  host.querySelector('[data-scan-filter]').addEventListener('input',()=>{page=1;render();},{signal});host.querySelector('[data-scan-status]').addEventListener('change',()=>{page=1;render();},{signal});
  host.querySelector('[data-scan-previous]').addEventListener('click',()=>{page--;render();},{signal});host.querySelector('[data-scan-next]').addEventListener('click',()=>{page++;render();},{signal});
  form.querySelector('[name=scope]').addEventListener('change',event=>{const all=event.target.value==='all';form.querySelector('[name=symbols]').required=!all;form.querySelector('[name=symbols]').disabled=all;},{signal});
  form.addEventListener('submit',async event=>{event.preventDefault();try{const scope=formValue(form,'scope'),model_id=formValue(form,'model_id');if(!model_id)throw new Error('请选择明确的冻结模型版本。');const symbols=scope==='all'?[]:symbolsFrom(formValue(form,'symbols'),6000),target=host.querySelector('[data-scan-job]');target.hidden=false;watcher?.stop();watcher=await submitJob(target,form.querySelector('[type=submit]'),'scan',{model_id,scope,symbols,end:formValue(form,'end')},{signal,onComplete:async job=>{if(job.result)showResult(job.result);else if(job.run_id)await openReport(job.run_id);if(job.error)showError(host,new Error(job.error));await loadReports();}});}catch(error){showError(host,error);}},{signal});
  async function loadReports(){try{const response=await api.reports(signal);if(signal.aborted)return;host.querySelector('[data-saved-scans]').innerHTML=reportList(arrayOf(response,'reports').filter(row=>row.family==='model_scan_v1'||['model_scan','scan'].includes(row.kind)),{limit:30});}catch(error){if(!signal.aborted)host.querySelector('[data-saved-scans]').innerHTML=errorHtml(error);}}
  host.querySelector('[data-refresh-saved]').addEventListener('click',loadReports,{signal});host.querySelector('[data-saved-scans]').addEventListener('click',event=>{const button=event.target.closest('[data-report-id]');if(button)openReport(button.dataset.reportId);},{signal});
  await Promise.all([loadReports(),bindModelSelector(host,{signal,selected:params.get('model_id') || ''}).catch(error=>showError(host,error))]);if(params.get('report'))await openReport(params.get('report'));
  return()=>watcher?.stop();
}
