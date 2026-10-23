/* Web training card on the prediction page: saved defaults, auto-replace on acceptance. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN');
  const active = new Set(['queued','running','cancelling']);
  const statuses = {queued:'等待开始',running:'运行中',succeeded:'已完成',failed:'失败',cancelling:'正在取消',cancelled:'已取消',partial:'部分完成',blocked:'条件未满足',interrupted:'服务中断',disabled:'自动更新已关闭',not_requested:'未申请替换',replaced:'已自动替换',kept:'保留现模型'};
  const state = {capabilities:null, parameters:null, plan:null, planFingerprint:null, jobs:new Map(), busy:false, timer:null, loaded:false};
  const keyOf = field => field.name || field.key || field.id;
  const fingerprint = value => JSON.stringify(Object.fromEntries(Object.keys(value).sort().map(key => [key,value[key]])));
  async function api(path, options = {}) {
    const headers = {...options.headers};
    if (options.body) headers['Content-Type'] = 'application/json';
    const token = sessionStorage.getItem('khquant_api_token');
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(path, {...options, headers, credentials:'same-origin'});
    const text = await response.text();
    let payload;
    try { payload = text ? JSON.parse(text) : {}; } catch (_) { throw new Error(`服务返回了无法解析的响应（${response.status}）。`); }
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : payload.detail?.message || payload.error || `请求失败（${response.status}）`);
    return payload;
  }
  function note(message) { $('web-training-state').textContent = message; }
  function setBusy(value) {
    state.busy = value;
    $('web-training-preflight').disabled = value;
    $('web-training-start').disabled = value || !(state.plan?.plan_id && state.plan.valid === true);
  }
  function stageName(value) {
    const labels = {source_verification:'核验因果来源',reference_labels:'生成纯价格方向目标',dataset_load:'读取已核验训练样本',reference_model:'训练Torch方向概率',model_select:'训练内部选择模型',calibration:'独立概率校准',validation:'验证预测基线与资产',completed:'完整训练完成',reference_source_verification:'核验可复用行情与因果特征',reference_dataset:'生成新纯价格目标',reference_training:'Torch参考模型训练',reference_calibration:'GPU概率校准',reference_policy:'核验逐股仓位政策',reference_validation:'留出预测与资产验证',reference_selection:'记录参考模型选择',calendar:'核验交易日历',raw_source:'冻结原始行情',raw_source_freeze:'冻结原始行情',native_source:'准备原生特征',native_source_prepare:'结构数据准备',data:'核验行情与日历',selection:'检查合格模型替换',queued:'等待训练资源',resource_wait:'等待上一任务释放计算资源'};
    return labels[value] || value || '等待阶段报告';
  }
  function renderPreflight(result) {
    const plan = result.plan || result, valid = result.valid === true && Boolean(plan.plan_id);
    const errors = result.errors || plan.errors || [], warnings = result.warnings || plan.warnings || [];
    state.plan = {...plan, valid};
    const host = $('web-training-preflight-result');
    host.hidden = false;
    host.innerHTML = `<h3>${valid ? '预检通过，可以开始训练' : '训练计划被阻断'}</h3>${errors.length ? `<ul class="training-blockers">${errors.map(item => `<li>${esc(typeof item === 'string' ? item : item.message || JSON.stringify(item))}</li>`).join('')}</ul>` : ''}${warnings.length ? `<ul class="training-warnings">${warnings.map(item => `<li>${esc(typeof item === 'string' ? item : item.message || JSON.stringify(item))}</li>`).join('')}</ul>` : ''}<dl><div><dt>历史合格并集</dt><dd>${number(plan.historical_union_count ?? plan.liquidity_universe?.selected_symbols?.length)} 股</dd></div><div><dt>比较现任</dt><dd>${esc(plan.incumbent_run_id || '无现任；初始化资格另行核验')}</dd></div><div><dt>日历</dt><dd>${esc(plan.calendar_run_id || '尚未核验')}</dd></div><div><dt>自动替换</dt><dd>已开启（验收效果高于当前即替换生效模型）</dd></div></dl>${plan.plan_id ? `<details><summary>冻结计划凭据</summary><code>${esc(plan.plan_id)}</code></details>` : ''}`;
    note(valid ? '预检通过；开始训练后新模型经研究验收，效果高于当前即自动替换。' : `预检未通过：${typeof errors[0] === 'string' ? errors[0] : errors[0]?.message || '未取得完整冻结计划凭据'}。训练保持阻断。`);
  }
  function renderJobs() {
    const host = $('web-training-jobs');
    const jobs = [...state.jobs.values()].sort((a,b) => String(b.created_at || b.job_id).localeCompare(String(a.created_at || a.job_id)));
    host.innerHTML = jobs.length ? jobs.map(job => {
      const detail = job.progress_detail || {}, result = job.result || {}, progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
      const selection = result.selection || {}, reasons = selection.reasons || result.blockers || [];
      const replacement = job.status === 'succeeded' ? `<p>训练已完成；替换结果：${esc(statuses[selection.status || result.comparison_status] || selection.status || result.comparison_status || '以资格报告为准')}。</p>` : '';
      return `<article class="training-job"><div class="task-heading"><strong>完整模型训练</strong><span class="badge">${esc(statuses[job.status] || job.status || '读取中')}</span></div><small>${esc(job.run_id || job.job_id)}</small><div class="progress-track"><i style="width:${progress}%"></i></div><p>${active.has(job.status) ? `当前阶段 ${progress}% · ` : ''}${esc(stageName(detail.stage))}${detail.current != null && Number(detail.total) > 0 ? ` · ${number(detail.current)} / ${number(detail.total)}` : ''}</p>${job.error ? `<p class="training-message error">${esc(job.error)}</p>` : ''}${replacement}${reasons.length ? `<ul class="training-blockers">${reasons.map(reason => `<li>${esc(typeof reason === 'string' ? reason : reason.message || JSON.stringify(reason))}</li>`).join('')}</ul>` : ''}<div class="button-group">${active.has(job.status) ? `<button type="button" data-web-training-cancel="${esc(job.job_id)}" ${job.status === 'cancelling' ? 'disabled' : ''}>取消训练</button>` : ''}<a class="button-link" href="#training">训练详情与历史</a></div></article>`;
    }).join('') : '';
  }
  function renderStatus(status) {
    const host = $('web-training-status');
    const current = status?.active_model;
    host.innerHTML = current?.run_id
      ? `<p>当前生效参考模型 <strong>${esc(current.run_id)}</strong>${current.selected_at ? ` · 生效 ${esc(current.selected_at)}` : ''}。新训练经研究验收效果更高即自动替换；未通过验收保留现模型。</p>`
      : '<p>尚无生效参考模型；首次训练通过验收后自动成为当前模型。</p>';
  }
  async function renderEvents() {
    const response = await api('/api/training/selection-events'), items = Array.isArray(response) ? response : response.events || response.items || [];
    $('web-training-selection-events').innerHTML = items.length ? items.slice(0, 5).map(item => { const payload = item.payload || item; return `<article><strong>${esc(({select:'自动替换为当前模型',rollback:'回滚当前研究选择'})[item.kind] || item.action || item.event_type || '模型选择记录')}</strong><span>记录 ${esc(item.created_at || payload.recorded_at || payload.selected_at || '')}${payload.effective_from ? ` · 生效 ${esc(payload.effective_from)}` : ''}</span><p>${esc(payload.reason || payload.reason_code || '通过冻结证据检查后记录')}</p><small>模型 ${esc(payload.run_id || payload.model_run_id || '无模型状态')}${payload.previous_run_id ? ` · 前任 ${esc(payload.previous_run_id)}` : ''}</small></article>`; }).join('') : '<p class="subtle">尚无自动替换事件；训练完成不会自动伪造选择或发布记录。</p>';
  }
  async function pollJobs() {
    if (document.hidden) { clearTimeout(state.timer); state.timer = setTimeout(() => pollJobs().catch(() => {}), 15000); return; }
    clearTimeout(state.timer);
    const running = [...state.jobs.values()].filter(job => active.has(job.status));
    for (const job of running) { try { state.jobs.set(job.job_id, await api(`/api/tasks/${encodeURIComponent(job.job_id)}`)); } catch (error) { note(error.message); } }
    renderJobs();
    if ([...state.jobs.values()].some(job => active.has(job.status))) state.timer = setTimeout(() => pollJobs().catch(error => note(error.message)), 2500);
    else if (running.length) { const status = await api('/api/training/status'); renderStatus(status); await renderEvents(); window.dispatchEvent(new Event('khquant-selection-updated')); }
  }
  async function refresh() {
    if (!$('web-training-card') || state.busy) return;
    const [capabilities, profiles, status] = await Promise.all([api('/api/training/capabilities'), api('/api/training/profiles'), api('/api/training/status')]);
    state.capabilities = capabilities;
    const fields = capabilities.schema?.fields || capabilities.parameter_schema?.fields || capabilities.fields || [];
    const saved = profiles.default_profile?.archived ? {} : profiles.default_profile?.parameters || {};
    const values = {...(capabilities.defaults || capabilities.builtin_profile?.parameters || {}), ...saved};
    // 参考架构不接受旧训练参数（如 auto_replace）：验收通过即自动替换生效参考模型，无需额外参数。
    state.parameters = Object.fromEntries((Array.isArray(fields) ? fields : []).map(field => [keyOf(field), values[keyOf(field)] ?? field.default ?? null]));
    renderStatus(status);
    for (const job of status.jobs || []) if (job.job_id) state.jobs.set(job.job_id, job);
    state.loaded = true;
    renderJobs();
    await renderEvents();
    await pollJobs();
  }
  async function preflight() {
    if (state.busy) return;
    // 初次预检可能先于成功刷新；refresh 在 busy 时会直接返回，须在置忙前完成。
    if (!state.parameters) await refresh().catch(error => note(`训练状态读取失败：${error.message}`));
    setBusy(true); state.plan = null; state.planFingerprint = null;
    try {
      if (!state.parameters) throw new Error('训练参数尚未读取，请刷新后重试。');
      const result = await api('/api/training/preflight', {method:'POST', body:JSON.stringify({parameters: state.parameters})});
      renderPreflight(result);
      state.planFingerprint = fingerprint(state.parameters);
    } catch (error) { note(`计划检查失败：${error.message}`); }
    finally { setBusy(false); }
  }
  async function start() {
    if (state.busy) return;
    try {
      if (!state.plan?.plan_id || state.plan.valid !== true || fingerprint(state.parameters) !== state.planFingerprint) throw new Error('训练参数或计划已变更，请重新检查训练计划。');
      setBusy(true);
      const job = await api('/api/training/jobs', {method:'POST', body:JSON.stringify({plan_id: state.plan.plan_id, parameters: state.parameters})});
      if (!job.job_id) throw new Error('服务未返回任务标识，不能确认训练已提交。');
      state.jobs.set(job.job_id, job);
      state.plan = null; state.planFingerprint = null;
      note(`训练任务已提交（任务号：${job.job_id}）；完成后按研究验收结果自动替换或保留现模型。再次训练需重新检查计划。`);
      renderJobs(); await pollJobs();
    } catch (error) { note(error.message); }
    finally { setBusy(false); }
  }
  function bind() {
    if (!$('web-training-card')) return;
    $('web-training-preflight').addEventListener('click', () => preflight().catch(error => note(error.message)));
    $('web-training-start').addEventListener('click', () => start().catch(error => note(error.message)));
    document.addEventListener('click', event => {
      const button = event.target.closest('button[data-web-training-cancel]');
      if (!button) return;
      api(`/api/tasks/${encodeURIComponent(button.dataset.webTrainingCancel)}/cancel`, {method:'POST', body:'{}'})
        .then(() => pollJobs()).catch(error => note(error.message));
    });
    window.addEventListener('hashchange', () => { if (location.hash.replace(/^#/, '').split('?')[0] === 'simulation') refresh().catch(error => note(error.message)); });
    refresh().catch(error => note(`训练状态读取失败：${error.message}`));
  }
  globalThis.KHQuantWebTraining = {refresh};
  bind();
})();
