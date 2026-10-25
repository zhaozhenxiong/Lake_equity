export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[character]));
export const arrayOf = (value, key) => Array.isArray(value) ? value : Array.isArray(value?.[key]) ? value[key] : [];
export const number = (value, digits = 2) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits}) : '—';
export const percent = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? `${number(Number(value) * 100, 2)}%` : '—';
export const day = value => typeof value === 'object' && value ? `${value.year}-${String(value.month).padStart(2, '0')}-${String(value.day).padStart(2, '0')}` : String(value || '').slice(0, 10);
export const today = () => new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date());
export const yearStart = () => `${today().slice(0, 4)}-01-01`;
export const nextNaturalDay = value => { const text = String(value ?? ''); if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return ''; const date = new Date(`${text}T00:00:00Z`); if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== text) return ''; date.setUTCDate(date.getUTCDate() + 1); return date.toISOString().slice(0, 10); };
export function dateTime(value) {
  if (!value) return '—';
  const text=String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
  const normalized=/[zZ]$|[+-]\d{2}:?\d{2}$/.test(text)?text:`${text.replace(' ','T')}+08:00`;
  const date=new Date(normalized);
  if (!Number.isFinite(date.getTime())) return text;
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date);
  const values=Object.fromEntries(parts.map(part=>[part.type,part.value]));
  return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
}
export const pretty = value => escapeHtml(JSON.stringify(value, null, 2));
export const activeJob = job => ['queued', 'pending', 'running', 'cancelling'].includes(job?.status);
export const jobId = job => job?.id || job?.job_id || '';
export const kindLabel = value => ({research_study:'十年研究',fusion_scan:'多因子与Kronos复核',trade_plan:'次日买卖计划',ten_year_model_study_v1:'十年研究报告',fusion_scan_v1:'融合选股报告',analysis:'结构分析', backtest:'模型策略回测', simulation:'模拟账户', scan:'模型选股', prediction:'Kronos 预测', training:'模型训练', factor_training:'统一模型训练',model_refit:'最终重拟合',frozen_factor_refit_v1:'最终重拟合报告',model_scan:'模型选股',model_backtest:'模型回测',model_scan_v1:'模型选股',model_backtest_v1:'模型回测',cross_sectional_return_v1:'训练实验', score_training:'评分训练', model_training:'完整模型训练', data_operation:'数据运维',update:'行情更新', import:'数据导入', board_features:'板块特征', board_sync:'板块同步', daily:'账户续算', score_observations:'预测观察'})[value] || value || '研究运行';
export const statusLabel = value => ({queued:'排队中', pending:'待处理', running:'运行中', cancelling:'取消中', cancelled:'已取消', succeeded:'已完成', success:'已完成', complete:'已完成', completed:'已完成', partial:'部分完成', failed:'失败', blocked:'已阻止', available:'可用', unavailable:'不可用', ready:'已就绪', missing:'未就绪', verified:'已核验'})[value] || value || '未知';

export function badge(value, label) {
  const tone = ['succeeded', 'success', 'complete', 'completed', 'available', 'verified', 'ready'].includes(value) ? 'good' : ['failed', 'blocked', 'unavailable', 'missing'].includes(value) ? 'bad' : ['partial', 'cancelled', 'cancelling'].includes(value) ? 'warn' : ['running', 'queued'].includes(value) ? 'teal' : '';
  return `<span class="badge ${tone}">${escapeHtml(label || statusLabel(value))}</span>`;
}

export function statusChip(value, label) {
  const tone = ['succeeded', 'success', 'complete', 'completed', 'available', 'verified', 'ready'].includes(value) ? 'good' : ['failed', 'blocked', 'unavailable', 'missing'].includes(value) ? 'bad' : ['partial', 'cancelled', 'cancelling'].includes(value) ? 'warn' : ['running', 'queued', 'pending'].includes(value) ? 'teal' : 'neutral';
  return `<span class="status-chip ${tone}"><i aria-hidden="true"></i>${escapeHtml(label || statusLabel(value) || '未运行')}</span>`;
}

// A/B/C/D tiers: prefer explicit tier data, otherwise derive from dataset coverage counts.
export function coverageTiers(coverage) {
  const source = coverage?.tiers || coverage?.by_tier || coverage?.tier_counts;
  const labels = {A:'完整', B:'部分', C:'未知或失败', D:'未安排'};
  if (Array.isArray(source)) {
    return ['A','B','C','D'].map((key,index) => {
      const item = source[index] || {};
      return {key:item.key || key, label:item.label || labels[key], value:Number(item.value ?? item.count ?? (typeof item === 'number' ? item : 0)), tone:item.tone || (key==='A'?'good':key==='B'?'warn':key==='C'?'bad':'neutral')};
    });
  }
  if (source && typeof source === 'object') {
    return ['A','B','C','D'].map(key => {
      const item = source[key];
      return {key, label:item?.label || labels[key], value:Number(item?.count ?? item ?? 0), tone:item?.tone || (key==='A'?'good':key==='B'?'warn':key==='C'?'bad':'neutral')};
    });
  }
  const counts = coverage?.counts || {};
  return [
    {key:'A', label:labels.A, value:Number(counts.complete || 0), tone:'good'},
    {key:'B', label:labels.B, value:Number(counts.partial || 0), tone:'warn'},
    {key:'C', label:labels.C, value:Number(counts.unknown || 0) + Number(counts.failed || 0), tone:'bad'},
    {key:'D', label:labels.D, value:Number(counts.not_scheduled || 0) + Number(counts.not_enabled || 0), tone:'neutral'}
  ];
}

export function heading(title, description, actions = '') {
  return `<div class="page-heading"><div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(description)}</p></div>${actions ? `<div class="heading-actions">${actions}</div>` : ''}</div><div data-page-error></div>`;
}

export function empty(title, description = '', action = '') {
  return `<div class="empty-state"><span class="empty-icon" aria-hidden="true">⌁</span><strong>${escapeHtml(title)}</strong><p>${escapeHtml(description)}</p>${action}</div>`;
}

export function errorHtml(error) {
  return `<div class="notice error" role="alert"><strong>${escapeHtml(error?.message || error)}</strong>${error?.status ? `<p>HTTP ${escapeHtml(error.status)} · 检查输入、数据覆盖或任务详情后重试。</p>` : ''}</div>`;
}

// One shared mapping from backend failure text to a concrete next action; empty when nothing matches.
export function errorAdvice(message, {cutoff = ''}={}) {
  const text = String(message ?? '');
  if (!text) return '';
  const quoted = text.match(/\b\d{4}-\d{2}-\d{2}\b/);
  const next = nextNaturalDay(cutoff || (quoted ? quoted[0] : ''));
  if (/信息截止|information[ _]?cutoff|training and validation information|不得早于|须晚于|开始日期|begin after/i.test(text)) return `${next ? `开始日改为 ${next}` : '开始日改为信息截止后的下一自然日'}，或使用历史回放。`;
  if (/最多\s*200|200\s*只|关注池.*200/.test(text)) return '关注池最多 200 只股票；请先移除部分股票或缩小勾选范围。';
  if (/(股票池|symbols|关注).*(空|至少|empty|required)|(空|至少).*(股票池|symbols)|empty (explicit )?(pool|symbols)|请填写股票代码/i.test(text)) return '在关注页勾选股票或使用全选后重试。';
  if (/1000|上限|cap|max_members|rank_beyond_cap/i.test(text)) return '勾选 explicit_symbols（显式股票池）或先查看有效池。';
  if (/must lie within the saved out-of-sample series|saved out-of-sample series/i.test(text)) return '回测区间须落在原序列区间内：结束日期改为原序列上限（或更早），开始日期改为原序列起点（或更晚）。要看更新后的交易日请改用「固定模型」短区间；要让长区间包含新日期需重跑十年研究协议。';
  if (/序列|series|区间|window|members/i.test(text)) return '查看原序列区间后调整开始/结束日期。';
  return '';
}

export function showError(host, error) {
  if (error?.name === 'AbortError' || !host?.isConnected) return;
  const target = host.querySelector('[data-page-error]') || host;
  target.innerHTML = errorHtml(error);
}

let notificationTimer;
export function notify(message, tone = '') {
  const host = document.getElementById('notification');
  if (!host) return;
  clearTimeout(notificationTimer);
  host.textContent = message;
  host.className = `notification ${tone}`;
  host.hidden = false;
  notificationTimer = setTimeout(() => { host.hidden = true; }, 6000);
}

export async function busy(button, operation, label = '正在提交…') {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { return await operation(); }
  finally { button.disabled = false; button.textContent = original; }
}

export function symbolsFrom(value, maximum = 10) {
  const symbols = [...new Set(String(value || '').split(/[\s,，;；]+/).map(item => item.trim().toUpperCase()).filter(Boolean))];
  if (!symbols.length) throw new Error('请填写股票代码，例如 600519.SH。');
  if (symbols.length > maximum) throw new Error(`本次最多 ${maximum} 只股票，已填写 ${symbols.length} 只。请缩小范围。`);
  if (symbols.some(symbol => !/^(?:[036]\d{5}\.(?:SH|SZ)|\d{6})$/.test(symbol))) throw new Error('使用六位沪深股票代码，推荐带 .SH 或 .SZ 交易所后缀。');
  return symbols;
}

export function validateDates(start, end) {
  if (start && end && start > end) throw new Error('开始日期不得晚于结束日期。');
}

export function table(columns, rows, options = {}) {
  if (!rows.length) return empty(options.emptyTitle || '暂无记录', options.emptyDescription || '选择范围并读取数据，或先完成对应研究运行。');
  return `<div class="table-wrap"><table class="${escapeHtml(options.className || '')}"><thead><tr>${columns.map(column => `<th scope="col">${escapeHtml(column.label)}</th>`).join('')}</tr></thead><tbody>${rows.map((row, index) => `<tr>${columns.map(column => `<td${column.wrap ? ' class="wrap"' : ''}${column.title ? ` title="${escapeHtml(column.title(row))}"` : ''}>${column.render ? column.render(row, index) : escapeHtml(row[column.key] ?? '—')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

export function dataTable(rows, limit = 100) {
  if (!rows.length) return empty('该范围没有数据', '检查代码、日期和数据集覆盖；缺失不会替换成模拟数据。');
  const columns = [...new Set(rows.slice(0, limit).flatMap(row => Object.keys(row)))];
  return table(columns.map(key => ({key, label:key, title:row => typeof row[key] === 'object' ? JSON.stringify(row[key]) : row[key], render:row => escapeHtml(typeof row[key] === 'object' && row[key] !== null ? JSON.stringify(row[key]) : row[key] ?? '—')})), rows.slice(0, limit));
}

export function disclosure(title, data, open = false) {
  return `<details class="json-disclosure"${open ? ' open' : ''}><summary>${escapeHtml(title)}</summary><pre>${pretty(data)}</pre></details>`;
}

export function fullReportLink(report){
  const url=report?.dashboard_view?.full_report_url;
  if(typeof url!=='string'||!url.startsWith('/api/v1/'))return '';
  return `<div class="button-row"><a class="button small-button" href="${escapeHtml(url)}" download>下载完整报告 JSON</a><span class="small muted">页面显示指标摘要；完整逐日账本按需下载。</span></div>`;
}

export function downloadJson(data, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function progressValue(job) {
  if(job?.kind==='research_study')return null; // Saved folds belong to one protocol, not the whole study.
  const progress = job?.progress;
  if (typeof progress === 'number') return Math.max(0, Math.min(100, progress));
  if (progress?.percent!==null && progress?.percent!==undefined && Number.isFinite(Number(progress.percent))) return Math.max(0, Math.min(100, Number(progress.percent)));
  const total = progress?.total ?? job?.progress_detail?.total;
  const current = progress?.current ?? job?.progress_detail?.current;
  return total > 0 && Number.isFinite(Number(current)) ? Math.max(0, Math.min(100, Number(current) / Number(total) * 100)) : null;
}

export function formValue(form, name) { return new FormData(form).get(name)?.toString().trim() || ''; }

export function routeLink(page, values = {}) {
  const query = new URLSearchParams(Object.entries(values).filter(([, value]) => value !== undefined && value !== null && value !== ''));
  return `#${page}${query.size ? `?${query}` : ''}`;
}

export function bindSymbolSearch(input, signal) {
  if (!input) return;
  let timer;
  let sequence = 0;
  const list = document.createElement('datalist');
  list.id = `${input.id || 'symbol'}-options`;
  input.after(list);
  input.setAttribute('list', list.id);
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const current = ++sequence;
    if (input.value.trim().length < 2) return;
    timer = setTimeout(async () => {
      try {
        const {api} = await import('./api.js');
        const response = await api.symbols({q:input.value.trim(), limit:15}, signal);
        if (current !== sequence || signal.aborted) return;
        list.innerHTML = arrayOf(response, 'symbols').map(row => `<option value="${escapeHtml(row.symbol)}">${escapeHtml(row.name || '')}</option>`).join('');
      } catch (error) { if (error.name !== 'AbortError') list.replaceChildren(); }
    }, 220);
  }, {signal});
  signal.addEventListener('abort', () => clearTimeout(timer), {once:true});
}
