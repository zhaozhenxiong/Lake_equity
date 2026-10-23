import {api} from '../core/api.js';
import {arrayOf,badge,busy,dataTable,disclosure,downloadJson,empty,errorHtml,escapeHtml,formValue,heading,number,showError,table,today,validateDates} from '../core/ui.js';
import {submitJob} from '../core/jobs.js';
import {mountDataOps} from '../core/data-ops.js';
import {mountDataInspection} from '../core/data-inspection.js';

export async function mount(host,{signal,params}) {
  let datasets=[];
  let result;
  let watcher;
  host.innerHTML=heading('数据湖','浏览已登记数据集，核对实际覆盖，并用有界查询检查研究输入。',`<a class="button" href="#data?tab=operations">数据运维</a>`)+`<section class="panel"><div class="panel-header"><div><h2>数据集目录</h2><p data-catalog-summary>正在读取登记与本地状态…</p></div><span class="badge blue">CNEquity</span></div><div class="filter-bar"><label class="field-inline">搜索<input id="dataset-search" placeholder="数据集名称或说明" aria-label="搜索数据集"></label><label class="field-inline">分层<select id="dataset-layer"><option value="">全部层</option></select></label><label class="field-inline">本地状态<select id="dataset-availability"><option value="">全部</option><option value="ready">已有数据</option><option value="empty">尚无数据</option></select></label><span class="coverage-count" data-filter-count></span></div><div data-catalog-table><div class="loading-state">正在读取目录…</div></div><div class="table-footer"><span>覆盖日期按数据集实际返回；没有覆盖证明时保留未知。</span><div class="button-row"><button class="small-button" data-page-previous>上一页</button><span data-page-number></span><button class="small-button" data-page-next>下一页</button></div></div></section><section class="panel space-top query-workspace" id="dataset-query"><div class="panel-header"><div><h2>读取数据与凭证</h2><p>每次最多返回指定行数，保留真实读取身份</p></div><button type="button" class="small-button" data-export-query disabled>导出当前查询</button></div><form id="dataset-query-form" class="form-toolbar"><div class="form-grid"><label class="span-two">数据集<select name="dataset" id="query-dataset" required><option value="">选择数据集</option></select></label><label>股票代码<input name="symbol" placeholder="600519.SH（可选）"></label><label>最多行数<select name="limit"><option>100</option><option>200</option><option>500</option></select></label><label>开始日期<input type="date" name="start"></label><label>结束日期<input type="date" name="end"></label><label>可知截至<input type="date" name="as_of"></label><button class="primary" type="submit">读取数据</button></div><p class="form-note" data-query-description>选择数据集后查看说明。日期和代码是否适用，以该数据集的查询合同为准。</p></form><div data-query-output>${empty('选择一份数据开始检查','这里只读取数据；不会自动采集或启动全市场任务。')}</div></section><section class="panel space-top"><div class="panel-header"><div><h2>显式更新与导入</h2><p>提交后在任务页查看进度与实际结果</p></div></div><div class="panel-body"><form id="data-operation-form" class="form-grid"><label class="span-two">数据操作<select name="kind"><option value="update">执行配置中的 CNEquity 日更</option><option value="import">全量导入已有原始行情库</option></select></label><button class="primary" type="submit">提交所选数据任务</button><p class="form-note span-all">日更按本地配置处理实际人口；导入会核验原始行情全库并发布新数据版本。两项均为显式长任务，可能耗时较长，可在任务页取消。</p></form></div><div class="job-inline" data-data-job hidden></div></section>`;
  host.insertAdjacentHTML('beforeend',`<section class="panel space-top"><div class="panel-header"><div><h2>质量与来源核验</h2><p>质量运行、跨来源差异与隔离记录，完整性资格独立显示</p></div><button type="button" class="small-button" data-refresh-quality>刷新质量</button></div><div data-quality-output><div class="loading-state">正在读取已保存质量记录…</div></div></section>`);
  async function loadQuality(){
    const target=host.querySelector('[data-quality-output]');
    try{
      const quality=await api.quality(signal);if(signal.aborted)return;
      const findings=arrayOf(quality,'findings_runs');
      target.innerHTML=`<div class="panel-body"><div class="button-row">${badge(quality.strict_history_eligible===true?'verified':'unknown',quality.strict_history_eligible===true?'严格历史资格通过':'严格历史资格未授予')}<span class="small muted">历史完整性：${escapeHtml(quality.historical_completeness || '未返回')}</span></div><p class="risk-note">采集成功、最大日期和已发布版本各自是证据，不能代替研究人口与交易日连续性核验。</p></div>${table([{label:'质量运行',key:'run_id'},{label:'行情日期',key:'trade_date'},{label:'问题数',render:row=>number(row.total,0)},{label:'严重级别',render:row=>escapeHtml(Object.entries(row.by_severity || {}).map(([key,value])=>`${key}: ${value}`).join(' / ') || '—')},{label:'主要检查',render:row=>escapeHtml((row.top_checks || []).map(row=>row.join(': ')).join('；')),wrap:true}],findings,{emptyTitle:'尚无已保存的质量运行',emptyDescription:'没有质量记录不会显示为检查通过。运行采集或来源核验后再查看。'})}${disclosure('跨来源差异、隔离与迁移证据',{diff_runs:quality.diff_runs,quarantine:quality.quarantine,legacy_imports:quality.legacy_imports,on_demand:quality.on_demand})}`;
      target.querySelectorAll('tbody tr').forEach((row,index)=>{const id=findings[index]?.run_id;if(id&&row.cells[0])row.cells[0].innerHTML=`<button type="button" class="row-link" data-quality-run="${escapeHtml(id)}">${escapeHtml(id)}</button>`;});
    }catch(error){if(error.name!=='AbortError')target.innerHTML=errorHtml(error);}
  }
  host.querySelector('[data-refresh-quality]').addEventListener('click',loadQuality,{signal});
  const queryGrid=host.querySelector('#dataset-query-form .form-grid');
  queryGrid.querySelector('[type=submit]').insertAdjacentHTML('beforebegin',`<label>价格处理<select name="adjust"><option value="">原始未复权</option><option value="qfq">前复权</option><option value="hfq">后复权</option><option value="total_return">总回报</option></select></label><label>可得时间模式<select name="pit_mode"><option value="strict">严格历史可知</option><option value="best_effort">尽力读取（需审查凭证）</option></select></label>`);
  host.querySelector('[data-quality-output]').insertAdjacentHTML('afterend',`<div class="panel-body"><form data-quality-detail-form class="form-grid"><label class="span-two">质量运行身份<input name="run_id" placeholder="选择上方运行身份或输入完整 run_id" required></label><button type="submit">读取质量与差异明细</button></form><div data-quality-detail></div></div>`);
  host.querySelector('[data-quality-detail-form]').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,target=host.querySelector('[data-quality-detail]');try{const value=await busy(form.querySelector('[type=submit]'),()=>api.qualityRun(formValue(form,'run_id'),signal),'读取中…');if(signal.aborted)return;target.innerHTML=`<h3>${escapeHtml(value.run_id)}</h3><p>行情日期：${escapeHtml(value.trade_date || '未返回')}</p>${dataTable(value.findings || [],500)}${disclosure('来源差异明细',value.diffs)}`;}catch(error){if(error.name!=='AbortError')target.innerHTML=errorHtml(error);}},{signal});
  host.querySelector('[data-quality-output]').addEventListener('click',event=>{const button=event.target.closest('[data-quality-run]');if(button){const form=host.querySelector('[data-quality-detail-form]');form.querySelector('[name=run_id]').value=button.dataset.qualityRun;form.requestSubmit();}},{signal});
  let page=0;
  const pageSize=12;
  function selected(){return datasets.find(dataset=>dataset.name===host.querySelector('[name=dataset]').value);}
  function describe(){const dataset=selected();host.querySelector('[data-query-description]').textContent=dataset?.description || dataset?.title || '按数据集支持的字段读取。没有匹配行时不会替换为样例数据。';}
  function renderCatalog(){
    const query=host.querySelector('#dataset-search').value.trim().toLowerCase();
    const layer=host.querySelector('#dataset-layer').value;
    const availability=host.querySelector('#dataset-availability').value;
    const filtered=datasets.filter(dataset=>(!query||`${dataset.name} ${dataset.description || ''} ${dataset.title || ''}`.toLowerCase().includes(query))&&(!layer||(dataset.layer || dataset.tier || '未分类')===layer)&&(!availability||(availability==='ready'?dataset.has_data===true:dataset.has_data!==true)));
    page=Math.min(page,Math.max(0,Math.ceil(filtered.length/pageSize)-1));
    host.querySelector('[data-filter-count]').textContent=`${filtered.length} 个数据集`;
    host.querySelector('[data-page-number]').textContent=`${page+1} / ${Math.max(1,Math.ceil(filtered.length/pageSize))}`;
    host.querySelector('[data-page-previous]').disabled=page===0;
    host.querySelector('[data-page-next]').disabled=(page+1)*pageSize>=filtered.length;
    host.querySelector('[data-catalog-table]').innerHTML=table([{label:'数据集',render:row=>`<button class="row-link" data-select-dataset="${escapeHtml(row.name)}">${escapeHtml(row.title || row.name)}</button>${row.title?`<div class="dataset-name">${escapeHtml(row.name)}</div>`:''}`},{label:'分层',render:row=>escapeHtml(row.layer || row.tier || '未分类')},{label:'本地状态',render:row=>badge(row.has_data===true?'available':'missing',row.has_data===true?'已有数据':'尚无数据')},{label:'覆盖范围',render:row=>`${escapeHtml(row.coverage_start || row.start || '—')} 至 ${escapeHtml(row.coverage_end || row.end || '—')}`},{label:'说明',key:'description',wrap:true},{label:'操作',render:row=>`<button class="small-button" data-select-dataset="${escapeHtml(row.name)}">查询</button>`}],filtered.slice(page*pageSize,(page+1)*pageSize),{className:'catalog-table',emptyTitle:'没有匹配的数据集',emptyDescription:'调整关键词、分层或本地状态筛选。'});
  }
  for(const id of ['dataset-search','dataset-layer','dataset-availability'])host.querySelector(`#${id}`).addEventListener(id==='dataset-search'?'input':'change',()=>{page=0;renderCatalog();},{signal});
  host.querySelector('[data-page-previous]').addEventListener('click',()=>{page--;renderCatalog();},{signal});
  host.querySelector('[data-page-next]').addEventListener('click',()=>{page++;renderCatalog();},{signal});
  host.addEventListener('click',event=>{const button=event.target.closest('[data-select-dataset]');if(button){host.querySelector('#query-dataset').value=button.dataset.selectDataset;describe();host.querySelector('#dataset-query').scrollIntoView({behavior:'smooth',block:'start'});}},{signal});
  host.querySelector('#query-dataset').addEventListener('change',describe,{signal});
  host.querySelector('#dataset-query-form').addEventListener('submit',async event=>{
    event.preventDefault();
    const form=event.currentTarget,output=host.querySelector('[data-query-output]');
    try{
      const start=formValue(form,'start'),end=formValue(form,'end');validateDates(start,end);
      result=await busy(form.querySelector('[type=submit]'),()=>api.dataset(formValue(form,'dataset'),{symbol:formValue(form,'symbol'),start,end,as_of:formValue(form,'as_of'),adjust:formValue(form,'adjust'),pit_mode:formValue(form,'pit_mode'),limit:Number(formValue(form,'limit'))},signal),'正在读取…');
      if(signal.aborted)return;
      const rows=arrayOf(result,'rows');
      output.innerHTML=`<div class="table-footer"><span>返回 ${number(result.returned_count ?? rows.length,0)} 行 / 匹配 ${number(result.count ?? rows.length,0)} 行</span><span>${escapeHtml(formValue(form,'dataset'))}</span></div>${dataTable(rows,500)}${disclosure('读取凭证与质量状态',result.receipt || {receipt:result.receipt,quality:result.quality,warning:'服务未返回读取凭证'})}`;
      host.querySelector('[data-export-query]').disabled=false;
    }catch(error){if(error.name!=='AbortError')output.innerHTML=errorHtml(error);}
  },{signal});
  host.querySelector('[data-export-query]').addEventListener('click',()=>{if(result)downloadJson(result,`lake-equity-${selected()?.name || 'query'}.json`);},{signal});
  host.querySelector('#data-operation-form').addEventListener('submit',async event=>{
    event.preventDefault();const form=event.currentTarget;
    try{
      const target=host.querySelector('[data-data-job]');target.hidden=false;watcher?.stop();
      watcher=await submitJob(target,form.querySelector('[type=submit]'),formValue(form,'kind'),{},{signal});
    }catch(error){showError(host,error);}
  },{signal});
  try{
    const response=await api.catalog(signal);if(signal.aborted)return()=>{};
    datasets=arrayOf(response,'datasets');
    host.querySelector('[data-catalog-summary]').textContent=`${datasets.length} 个已登记数据集，${datasets.filter(item=>item.has_data===true).length} 个已有本地数据`;
    host.querySelector('#query-dataset').innerHTML=`<option value="">选择数据集</option>${datasets.map(dataset=>`<option value="${escapeHtml(dataset.name)}">${escapeHtml(dataset.title || dataset.name)}</option>`).join('')}`;
    const layers=[...new Set(datasets.map(dataset=>dataset.layer || dataset.tier || '未分类'))];
    host.querySelector('#dataset-layer').innerHTML=`<option value="">全部层</option>${layers.map(layer=>`<option>${escapeHtml(layer)}</option>`).join('')}`;
    if(params.get('dataset')){host.querySelector('#query-dataset').value=params.get('dataset');describe();}
    renderCatalog();
  }catch(error){showError(host,error);host.querySelector('[data-catalog-table]').innerHTML=empty('目录读取失败','检查服务连接后刷新。');}
  await loadQuality();
  mountDataInspection(host,{signal,datasets});
  const stopOps=await mountDataOps(host,{signal});
  if(params.get('tab')==='operations')host.querySelector('#data-operations')?.scrollIntoView();
  return()=>{watcher?.stop();stopOps();};
}
