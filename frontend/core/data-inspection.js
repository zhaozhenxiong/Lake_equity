import {api} from './api.js';
import {arrayOf,busy,dataTable,disclosure,empty,errorHtml,escapeHtml,formValue,number,table} from './ui.js';

const coverageLabels={covered:'分区覆盖',gap:'待核对缺口','outside coverage':'覆盖区间外',unpartitioned:'非分区数据'};

export function heatmapHtml(value){
  const days=value.days || [],legend=value.legend || {};
  return `<p class="risk-note">颜色仅表示返回的分区覆盖状态。月度、年度数据按其原始粒度解释，不能据此认定逐日观测完整。</p><p class="small muted">${Object.entries(legend).map(([key,label])=>`${escapeHtml(key===' '?'空格':key)}：${escapeHtml(coverageLabels[label] || label)}`).join('；')}</p>${table([{label:'数据集',key:'dataset'},{label:'原始粒度',key:'granularity'},{label:'缺口语义',key:'gap_meaning'},...days.map((day,index)=>({label:day,render:row=>{const code=(row.cells || '')[index] ?? ' ',state=legend[code] || '未知',label=coverageLabels[state] || state;return `<span class="coverage-cell" data-coverage-state="${escapeHtml(state)}" title="${escapeHtml(`${day} · ${label} · 原始粒度 ${row.granularity || '未返回'}`)}">${escapeHtml(code===' '?'·':code)}</span>`;}}))],arrayOf(value,'rows'))}`;
}

function pagedTable(target,columns,rows,options={}){
  let page=0;const pageSize=50;
  function draw(){target.innerHTML=`${table(columns,rows.slice(page*pageSize,(page+1)*pageSize),options)}<div class="table-footer"><span>共 ${number(rows.length,0)} 项，第 ${page+1} / ${Math.max(1,Math.ceil(rows.length/pageSize))} 页</span><div class="button-row"><button class="small-button" data-inspect-prev ${page===0?'disabled':''}>上一页</button><button class="small-button" data-inspect-next ${(page+1)*pageSize>=rows.length?'disabled':''}>下一页</button></div></div>`;target.querySelector('[data-inspect-prev]').onclick=()=>{page--;draw();};target.querySelector('[data-inspect-next]').onclick=()=>{page++;draw();};}
  draw();
}

export function mountDataInspection(host,{signal,datasets=[]}){
  host.insertAdjacentHTML('beforeend',`<section class="panel space-top" id="data-inspection"><div class="panel-header"><div><h2>数据检查与采集凭证</h2><p>按需读取真实结构、分区、来源与历史运行；不会触发采集或统计刷新</p></div></div><div class="panel-body"><form data-inspection-form class="form-grid"><label>数据集<select name="dataset">${datasets.map(row=>`<option value="${escapeHtml(row.name)}">${escapeHtml(row.title || row.name)}</option>`).join('')}</select></label><label>检查内容<select name="view"><option value="detail">结构与覆盖</option><option value="partitions">分区</option><option value="dates">可选日期</option><option value="provenance">来源汇总</option><option value="series">来源时间序列</option><option value="runs">采集运行</option><option value="heatmap">全数据集覆盖图</option></select></label><button type="submit">读取检查结果</button><p class="form-note span-all">分区、日期与来源列表完整分页；采集运行按服务端分页读取。</p><label data-run-control hidden>运行状态<input name="status" placeholder="failed（可选）"></label><label class="check-field" data-run-control hidden><input type="checkbox" name="open">仅未关闭运行</label><label data-heatmap-control hidden>覆盖交易日<select name="days"><option>14</option><option>30</option><option>60</option></select></label></form><div data-inspection-output class="space-top">${empty('选择要核对的检查内容','只读操作，不会重算或修改历史状态。')}</div><div data-inspection-detail></div></div></section>`);
  const form=host.querySelector('[data-inspection-form]'),output=host.querySelector('[data-inspection-output]'),detail=host.querySelector('[data-inspection-detail]');
  let runOffset=0,sequence=0;
  form.querySelector('[name=view]').addEventListener('change',()=>{const view=formValue(form,'view');form.querySelector('[name=dataset]').disabled=['runs','heatmap'].includes(view);form.querySelectorAll('[data-run-control]').forEach(row=>row.hidden=view!=='runs');form.querySelector('[data-heatmap-control]').hidden=view!=='heatmap';runOffset=0;},{signal});
  async function read(){
    const current=++sequence,name=formValue(form,'dataset'),view=formValue(form,'view');detail.innerHTML='';
    const methods={detail:api.datasetDetail,partitions:api.datasetPartitions,dates:api.datasetDates,provenance:api.datasetProvenance,series:api.datasetProvenanceSeries};
    const value=await (view==='runs'?api.dataRuns({limit:30,offset:runOffset,status:formValue(form,'status'),open:new FormData(form).has('open')?true:undefined},signal):view==='heatmap'?api.dataHeatmap({days:Number(formValue(form,'days'))},signal):methods[view](name,signal));
    if(signal.aborted||current!==sequence)return;
    if(view==='detail'){
      output.innerHTML=`<h3>${escapeHtml(value.dataset || name)}</h3>${table([{label:'分层',key:'tier'},{label:'粒度',key:'granularity'},{label:'实际行数',render:row=>number(row.row_count,0)},{label:'逻辑字节',render:row=>number(row.bytes,0)},{label:'当前新鲜度',key:'freshness'}],[value])}<p>主键：${escapeHtml((value.primary_key || []).join('、') || '未返回')}；分区列：${escapeHtml(value.partition_col || '未返回')}；已知覆盖：${escapeHtml(value.coverage_start || '未知')} 至 ${escapeHtml(value.coverage_end || '未知')}</p>${table([{label:'字段',key:'column'},{label:'类型',key:'dtype'}],value.schema || [])}${disclosure('缺口、来源版本与批次凭证',{gaps:value.gaps,earliest_available:value.earliest_available,revision_id:value.revision_id,contract_fingerprint:value.contract_fingerprint,batches:value.batches,commands:value.commands})}`;
    }else if(view==='partitions')pagedTable(output,[{label:'分区',key:'partition'},{label:'原始粒度',key:'granularity'},{label:'起始',key:'period_start'},{label:'结束',key:'period_end'},{label:'行数',render:row=>number(row.row_count,0)},{label:'逻辑字节',render:row=>number(row.bytes,0)}],arrayOf(value));
    else if(view==='dates'){output.innerHTML=`<p>${escapeHtml(value.column || '日期')} · ${escapeHtml(value.kind || '')} · 原始粒度 ${escapeHtml(value.granularity || '未返回')} · 共 ${number(value.total,0)} 项</p><p>${escapeHtml(value.note || '')}</p><div data-date-table></div>`;pagedTable(output.querySelector('[data-date-table]'),[{label:'已返回日期',key:'date'}],(value.values || []).map(date=>({date})));}
    else if(view==='provenance')pagedTable(output,[{label:'来源',key:'source'},{label:'数据版本',key:'data_version'},{label:'行数',render:row=>number(row.row_count,0)},{label:'首次采集',key:'fetched_at_min'},{label:'最后采集',key:'fetched_at_max'}],arrayOf(value));
    else if(view==='series'){output.innerHTML=`<p>时间桶：${escapeHtml(value.bucket || '未返回')}</p><div data-source-series></div>`;pagedTable(output.querySelector('[data-source-series]'),[{label:'期间',key:'period_start'},{label:'来源',key:'source'},{label:'数据版本',key:'data_version'},{label:'行数',render:row=>number(row.row_count,0)}],value.points || []);}
    else if(view==='heatmap')output.innerHTML=heatmapHtml(value);
    else{
      output.innerHTML=`${table([{label:'运行身份',render:row=>`<button class="row-link" data-data-run="${escapeHtml(row.run_id)}">${escapeHtml(row.run_id)}</button>`},{label:'任务',key:'job_name'},{label:'状态',key:'status'},{label:'开始',key:'started_at'},{label:'写入行数',render:row=>number(row.rows_written,0)},{label:'批次',render:row=>number(row.batches,0)},{label:'错误',key:'error_message',wrap:true}],value.runs || [])}<div class="table-footer"><span>匹配 ${number(value.total,0)} 项，从第 ${number(runOffset+1,0)} 项起</span><div class="button-row"><button class="small-button" data-runs-prev ${runOffset===0?'disabled':''}>上一页</button><button class="small-button" data-runs-next ${runOffset+30>=value.total?'disabled':''}>下一页</button></div></div>${disclosure('失败日更分组',value.failed_daily_groups)}`;
      output.querySelector('[data-runs-prev]').onclick=()=>loadRuns(-30);output.querySelector('[data-runs-next]').onclick=()=>loadRuns(30);
    }
  }
  async function loadRuns(delta){runOffset=Math.max(0,runOffset+delta);try{await read();}catch(error){if(error.name!=='AbortError')output.innerHTML=errorHtml(error);}}
  form.addEventListener('submit',async event=>{event.preventDefault();runOffset=0;try{await busy(form.querySelector('[type=submit]'),read,'读取中…');}catch(error){if(error.name!=='AbortError')output.innerHTML=errorHtml(error);}},{signal});
  output.addEventListener('click',async event=>{const button=event.target.closest('[data-data-run]');if(!button)return;try{const value=await busy(button,()=>api.dataRun(button.dataset.dataRun,signal),'读取中…');if(signal.aborted)return;detail.innerHTML=`<h3>采集运行 ${escapeHtml(button.dataset.dataRun)}</h3>${dataTable(value.batches || [],100)}${disclosure('完整运行凭证',value)}`;}catch(error){if(error.name!=='AbortError')detail.innerHTML=errorHtml(error);}},{signal});
}
