import {api} from './api.js';
import {activeJob,badge,busy,escapeHtml,jobId,kindLabel,notify,progressValue,routeLink,showError,statusLabel} from './ui.js';

export function cancellationNotice(job) {
  if (!job.cancel_note && typeof job.cancel_effective !== 'boolean') return '';
  const title=job.cancel_effective===false?'取消未生效':job.cancel_effective===true?'取消已生效':'取消处理说明';
  return `<div class="notice" role="status"><strong>${title}</strong>${job.cancel_note?`<p>${escapeHtml(job.cancel_note)}</p>`:''}</div>`;
}

export function jobSummary(job) {
  const progress=progressValue(job);
  return `<div class="job-inline-top"><div><strong>${escapeHtml(kindLabel(job.kind))}</strong> ${badge(job.status)}</div><a href="${escapeHtml(routeLink('tasks',{job:jobId(job)}))}">查看任务</a></div>${progress!==null?`<div class="progress"><span style="width:${progress}%"></span></div>`:''}<p>${escapeHtml(job.progress?.stage || job.progress_detail?.stage || job.error || jobId(job))}${progress!==null?` · ${Math.round(progress)}%`:''}</p>${cancellationNotice(job)}${activeJob(job)?`<button class="small-button danger space-top" data-cancel-inline="${escapeHtml(jobId(job))}">取消任务</button>`:''}`;
}

export function watchJob(host, initial, {signal,onComplete,onError}={}) {
  let job=initial;
  let timer;
  let completed=false;
  let stopped=false;
  function schedule(){if(!stopped && !signal?.aborted && activeJob(job))timer=setTimeout(poll,2000);}
  function render(){if(host.isConnected)host.innerHTML=jobSummary(job);}
  async function poll(){
    if(stopped || signal?.aborted || !host.isConnected)return;
    try{
      job=await api.job(jobId(job),signal);
      if(stopped || signal?.aborted)return;
      render();
      if(!activeJob(job) && !completed){completed=true;await onComplete?.(job);}
      else schedule();
    }catch(error){if(error.name!=='AbortError'){host.innerHTML=`<div class="notice error">${escapeHtml(error.message)}<p>任务可能仍在运行，请到任务页重新读取。</p></div>`;onError?.(error);}}
  }
  async function handleCancel(event){
    const button=event.target.closest('[data-cancel-inline]');
    if(!button)return;
    try{await busy(button,()=>api.cancel(button.dataset.cancelInline,signal),'正在取消…');notify('已提交取消请求，正在等待任务退出。');job=await api.job(jobId(job),signal);render();}catch(error){notify(error.message,'error');}
  }
  host.addEventListener('click',handleCancel,signal?{signal}:{});
  render();
  if(activeJob(job))schedule();
  else{completed=true;Promise.resolve(onComplete?.(job)).catch(error=>onError?.(error));}
  function stop(){stopped=true;clearTimeout(timer);host.removeEventListener('click',handleCancel);}
  signal?.addEventListener('abort',stop,{once:true});
  return {stop,get job(){return job;}};
}

export async function submitJob(host, button, kind, params, {signal,onComplete}={}) {
  const errorHost=host.closest('[data-page]') || host;
  const errorTarget=errorHost.querySelector('[data-page-error]');
  if(errorTarget)errorTarget.replaceChildren();
  try{
    const response=await busy(button,()=>api.submit(kind,params,signal));
    if(signal?.aborted)return;
    const job=response.job || response;
    if(!jobId(job))throw new Error('服务未返回任务标识，无法跟踪计算。请检查任务列表后再提交。');
    notify(`${kindLabel(kind)}已接单`);
    return watchJob(host,job,{signal,onComplete});
  }catch(error){showError(errorHost,error);throw error;}
}
