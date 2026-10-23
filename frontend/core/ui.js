export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[character]));
export const arrayOf = (value, key) => Array.isArray(value) ? value : Array.isArray(value?.[key]) ? value[key] : [];
export const number = (value, digits = 2) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits}) : '—';
export const percent = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? `${number(Number(value) * 100, 2)}%` : '—';
export const day = value => typeof value === 'object' && value ? `${value.year}-${String(value.month).padStart(2, '0')}-${String(value.day).padStart(2, '0')}` : String(value || '').slice(0, 10);
export const today = () => new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date());
export const yearStart = () => `${today().slice(0, 4)}-01-01`;
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
export const kindLabel = value => ({analysis:'结构分析', backtest:'模型策略回测', simulation:'模拟账户', scan:'模型选股', prediction:'Kronos 预测', training:'模型训练', factor_training:'统一模型训练',model_refit:'最终重拟合',frozen_factor_refit_v1:'最终重拟合报告',model_scan:'模型选股',model_backtest:'模型回测',model_scan_v1:'模型选股',model_backtest_v1:'模型回测',cross_sectional_return_v1:'训练实验', score_training:'评分训练', model_training:'完整模型训练', data_operation:'数据运维',update:'行情更新', import:'数据导入', board_features:'板块特征', board_sync:'板块同步', daily:'账户续算', score_observations:'预测观察'})[value] || value || '研究运行';
export const statusLabel = value => ({queued:'排队中', pending:'待处理', running:'运行中', cancelling:'取消中', cancelled:'已取消', succeeded:'已完成', success:'已完成', complete:'已完成', completed:'已完成', partial:'部分完成', failed:'失败', blocked:'已阻止', available:'可用', unavailable:'不可用', ready:'已就绪', missing:'未就绪', verified:'已核验'})[value] || value || '未知';

export function badge(value, label) {
  const tone = ['succeeded', 'success', 'complete', 'completed', 'available', 'verified', 'ready'].includes(value) ? 'good' : ['failed', 'blocked', 'unavailable', 'missing'].includes(value) ? 'bad' : ['partial', 'cancelled', 'cancelling'].includes(value) ? 'warn' : ['running', 'queued'].includes(value) ? 'teal' : '';
  return `<span class="badge ${tone}">${escapeHtml(label || statusLabel(value))}</span>`;
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

export function downloadJson(data, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function progressValue(job) {
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
