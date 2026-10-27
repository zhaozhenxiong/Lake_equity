import {api} from './api.js';
import {watchJob} from './jobs.js';
import {arrayOf,badge,busy,dateTime,empty,errorHtml,escapeHtml,jobId,number,table} from './ui.js';

export function storageBytes(value){
  if(typeof value!=='number'||!Number.isFinite(value)||value<0)return '未知';
  const units=['B','KiB','MiB','GiB','TiB'];let unit=0,size=value;
  while(size>=1024&&unit<units.length-1){size/=1024;unit++;}
  return `${number(size,unit?2:0)} ${units[unit]}`;
}

export function publishedDatasetBytes(snapshot,name){
  if(snapshot?.status!=='completed')return null;
  return arrayOf(snapshot,'datasets').find(row=>row.dataset===name)?.current_published_logical_bytes ?? null;
}

export function storageUsageHtml(value){
  const snapshot=value?.snapshot,attempt=value?.last_attempt,status=value?.status || 'not_measured';
  const labels={not_measured:'尚未统计',completed:'统计完成',partial:'本次部分完成',failed:'本次统计失败',cancelled:'本次已取消',running:'统计中',unavailable:'缓存不可用'};
  const limit=value?.limits;
  const state=`<div class="button-row">${badge(status,labels[status] || '状态待确认')}${snapshot?`<span class="small muted">统计时间 ${escapeHtml(dateTime(snapshot.finished_at))}${value.stale?' · 缓存已过期或本次未完整完成':''}</span>`:''}</div>${limit?`<p class="small muted space-top">单次最多检查 ${number(limit.max_files,0)} 个文件 / ${number(limit.max_seconds,0)} 秒；运行中可取消，超出预算会保留为部分结果。</p>`:''}`;
  const failure=value?.error || attempt?.error || attempt?.budget_reason;
  const notice=failure?`<div class="notice error space-top">${escapeHtml(failure)}</div>`:'';
  if(!snapshot)return state+notice+empty('尚无可展示的空间统计','点击“刷新大小”检查固定范围内的文件元数据；页面打开只读取缓存。');
  const totals=snapshot.totals || {},volume=arrayOf(snapshot,'volumes')[0];
  const retained=attempt?.scan_id&&attempt.scan_id!==snapshot.scan_id;
  const metrics=[['文件逻辑大小',storageBytes(totals.logical_path_bytes),'按文件路径累计，可能含多个硬链接名'],
    ['分配空间估算',storageBytes(totals.allocated_bytes),totals.allocated_bytes===null?`分配空间未完整取得；已知部分 ${storageBytes(totals.known_allocated_bytes)}`:'按文件身份去硬链接重'],
    ['文件 / 唯一文件',`${number(totals.file_count,0)} / ${totals.unique_file_count===null?'未知':number(totals.unique_file_count,0)}`,'同一硬链接目标只算一个唯一文件'],
    ['所在磁盘可用',storageBytes(volume?.free_bytes),volume?`${volume.volume} · 整卷容量 ${storageBytes(volume.total_bytes)}`:'卷空间未取得']];
  return state+notice+`${retained?'<p class="notice space-top">本次未完整完成，继续展示上次完整快照。</p>':''}${snapshot.status==='partial'?'<p class="notice space-top">以下仅为已检查范围，不能当作完整占用。</p>':''}<div class="metrics space-top">${metrics.map(([label,value,note])=>`<div class="metric"><div class="metric-label">${label}</div><div class="metric-value">${escapeHtml(value)}</div><div class="metric-detail">${escapeHtml(note)}</div></div>`).join('')}</div><p class="small muted">统计范围：${escapeHtml((snapshot.included_roots || []).join('、') || '未返回')}。分配空间是文件数据估算，不代表硬盘物理扇区或可回收空间。</p><details><summary>分层与数据集明细</summary><div class="space-top">${table([{label:'分层',key:'label'},{label:'文件',render:row=>number(row.file_count,0)},{label:'逻辑大小',render:row=>escapeHtml(storageBytes(row.logical_path_bytes))},{label:'分类独占分配',render:row=>escapeHtml(storageBytes(row.exclusive_allocated_bytes))},{label:'跨分类共享引用',render:row=>escapeHtml(storageBytes(row.shared_allocated_reference_bytes))}],arrayOf(snapshot,'categories'))}<p class="small muted">共享引用出现在相关分类中，不能直接相加；主合计已全局去重。</p>${table([{label:'数据集',key:'dataset'},{label:'已统计文件逻辑大小',render:row=>escapeHtml(storageBytes(row.logical_path_bytes))},{label:'当前发布逻辑大小',render:row=>escapeHtml(storageBytes(publishedDatasetBytes(snapshot,row.dataset)))}],arrayOf(snapshot,'datasets'),{emptyTitle:'当前范围没有可归属的数据集'})}<p class="small muted">未纳入：${escapeHtml((snapshot.excluded_roots || []).join('、'))}</p></div></details>${attempt?.error_count?`<details class="space-top"><summary>本次有 ${number(attempt.error_count,0)} 项未完整检查</summary>${table([{label:'相对路径',key:'path',wrap:true},{label:'原因',key:'reason'},{label:'说明',key:'detail',wrap:true}],arrayOf(attempt,'errors'))}</details>`:''}`;
}

export function mountStorageUsage(host,{signal,onSnapshot}={}){
  let scope='lake',sequence=0,watcher;
  host.innerHTML=`<section class="panel space-top data-storage-usage"><div class="panel-header"><div><h2>本地存储预览</h2><p>显示所选运行数据范围，不包含发行备份、验收产物或程序环境</p></div><div class="button-row"><label class="field-inline">范围<select data-usage-scope aria-label="存储统计范围"><option value="lake">数据湖</option><option value="runtime">数据湖与研究产物</option></select></label><button type="button" class="small-button" data-usage-refresh>刷新大小</button></div></div><div class="panel-body" data-usage-errors hidden></div><div class="panel-body" data-usage-content><div class="loading-state">读取已保存的空间统计…</div></div><div class="job-inline" data-usage-job hidden></div></section>`;
  const content=host.querySelector('[data-usage-content]'),errors=host.querySelector('[data-usage-errors]'),jobHost=host.querySelector('[data-usage-job]'),button=host.querySelector('[data-usage-refresh]');
  async function load(){
    const current=++sequence;
    try{
      const value=await api.storageUsage({scope},signal);
      if(signal?.aborted||current!==sequence)return;
      content.innerHTML=storageUsageHtml(value);onSnapshot?.(value);
    }catch(error){if(error.name!=='AbortError'&&current===sequence){errors.hidden=false;errors.innerHTML=errorHtml(error);}}
  }
  host.querySelector('[data-usage-scope]').addEventListener('change',event=>{
    scope=event.target.value;watcher?.stop();jobHost.hidden=true;button.disabled=false;errors.replaceChildren();errors.hidden=true;
    content.innerHTML='<div class="loading-state">读取所选范围的缓存…</div>';onSnapshot?.(null);load();
  },{signal});
  button.addEventListener('click',async()=>{
    const selected=scope;
    errors.replaceChildren();errors.hidden=true;
    try{
      const response=await busy(button,()=>api.refreshStorageUsage({scope:selected},signal),'接单中…');
      if(signal?.aborted||selected!==scope)return;
      const job=response.job || response;
      if(!jobId(job))throw new Error('服务未返回大小统计任务身份。');
      watcher?.stop();jobHost.hidden=false;button.disabled=true;
      watcher=watchJob(jobHost,job,{signal,
        onComplete:async()=>{if(selected===scope&&!signal?.aborted){button.disabled=false;await load();}},
        onError:()=>{if(selected===scope&&!signal?.aborted)button.disabled=false;}});
    }catch(error){if(error.name!=='AbortError'){errors.hidden=false;errors.innerHTML=errorHtml(error);}}
  },{signal});
  load();
  const stop=()=>{sequence++;watcher?.stop();};
  signal?.addEventListener('abort',stop,{once:true});
  return stop;
}
