const API_ROOT = '/api/v1';

export class ApiError extends Error {
  constructor(message, status, details) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

export function queryString(values = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  return query.size ? `?${query}` : '';
}

function errorMessage(body, status) {
  const detail = body?.detail ?? body?.error ?? body?.message;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => `${(item.loc || []).filter(part => part !== 'body').join('.')}: ${item.msg || JSON.stringify(item)}`).join('；');
  if (detail && typeof detail === 'object') return detail.message || JSON.stringify(detail);
  return `请求失败（HTTP ${status}）`;
}

export async function request(path, options = {}) {
  const {body, headers, apiRoot = API_ROOT, ...remaining} = options;
  const response = await fetch(`${apiRoot}${path}`, {
    credentials: 'same-origin',
    ...remaining,
    headers: {Accept: 'application/json', ...(body === undefined ? {} : {'Content-Type': 'application/json'}), ...headers},
    ...(body === undefined ? {} : {body: JSON.stringify(body)})
  });
  const contentType = response.headers.get('content-type') || '';
  const result = contentType.includes('json') ? await response.json() : await response.text();
  if (!response.ok) throw new ApiError(errorMessage(result, response.status), response.status, result);
  if (typeof result === 'string' && contentType.includes('text/html')) throw new ApiError('接口返回了页面，请检查服务路由配置。', response.status, null);
  return result;
}

export const api = {
  overview: signal => request('/overview', {signal}),
  catalog: signal => request('/catalog', {signal}),
  symbols: (values, signal) => request(`/symbols${queryString(values)}`, {signal}),
  dataset: (name, values, signal) => request(`/datasets/${encodeURIComponent(name)}/read${queryString(values)}`, {signal}),
  datasetDetail: (name,signal) => request(`/data/datasets/${encodeURIComponent(name)}`,{signal}),
  datasetPartitions: (name,signal) => request(`/data/datasets/${encodeURIComponent(name)}/partitions`,{signal}),
  datasetDates: (name,signal) => request(`/data/datasets/${encodeURIComponent(name)}/dates`,{signal}),
  datasetProvenance: (name,signal) => request(`/data/datasets/${encodeURIComponent(name)}/provenance`,{signal}),
  datasetProvenanceSeries: (name,signal) => request(`/data/datasets/${encodeURIComponent(name)}/provenance/series`,{signal}),
  qualityRun: (id,signal) => request(`/data/quality/runs/${encodeURIComponent(id)}`,{signal}),
  dataRuns: (values,signal) => request(`/data/runs${queryString(values)}`,{signal}),
  dataRun: (id,signal) => request(`/data/runs/${encodeURIComponent(id)}`,{signal}),
  dataHeatmap: (values,signal) => request(`/data/heatmap${queryString(values)}`,{signal}),
  features: (values, signal) => request(`/features${queryString(values)}`, {signal}),
  quality: signal => request('/quality', {signal}),
  marketStatus: signal => request('/market/status', {signal}),
  prepareMarket: (body, signal) => request('/market/prepare', {method:'POST', body, signal}),
  marketFeatures: (values, signal) => request(`/market/features${queryString(values)}`, {signal}),
  marketHistory: (id, values, signal) => request(`/market/boards/${encodeURIComponent(id)}/history${queryString(values)}`, {signal}),
  marketMembers: (id, values, signal) => request(`/market/boards/${encodeURIComponent(id)}/members${queryString(values)}`, {signal}),
  marketSpecialties: (values, signal) => request(`/market/specialties${queryString(values)}`, {signal}),
  analyze: (body, signal) => request('/analyze', {method: 'POST', body, signal}),
  jobs: signal => request('/jobs', {signal}),
  job: (id, signal) => request(`/jobs/${encodeURIComponent(id)}`, {signal}),
  submit: (kind, params, signal) => request('/jobs', {method: 'POST', body: {kind, params}, signal}),
  cancel: (id, signal) => request(`/jobs/${encodeURIComponent(id)}/cancel`, {method: 'POST', body: {}, signal}),
  reports: signal => request('/reports', {signal}),
  report: (id, signal) => request(`/reports/${encodeURIComponent(id)}`, {signal}),
  watchlist: signal => request('/watchlist', {signal}),
  accounts: signal => request('/accounts',{signal}),
  accountChartOptions: (values,signal) => request(`/accounts/chart-options${queryString(values)}`,{signal}),
  accountChart: (id,values,signal) => request(`/accounts/${encodeURIComponent(id)}/chart${queryString(values)}`,{signal}),
  addWatch: (symbol, signal) => request('/watchlist', {method: 'POST', body: {symbol}, signal}),
  deleteWatch: (symbol, signal) => request(`/watchlist/${encodeURIComponent(symbol)}`, {method: 'DELETE', signal}),
  models: signal => request('/models', {signal}),
  model: (id, signal) => request(`/models/${encodeURIComponent(id)}`, {signal}),
  learningPreflight: (body, signal) => request('/learning/preflight', {method:'POST',body,signal}),
  learningSeries: (id, signal) => request(`/learning/series/${encodeURIComponent(id)}`, {signal}),
  learningCatalog: signal => request('/learning/catalog', {signal}),
  learningRuns: signal => request('/learning/runs', {signal}),
  learningRun: (id, signal) => request(`/learning/runs/${encodeURIComponent(id)}`, {signal}),
  predictionChart: (reference, signal) => request(`/predictions/${encodeURIComponent(reference)}/chart`, {signal}),
  operations: signal => request('/data/operations',{signal}),
  operationPreview: (body,signal) => request('/data/operations/preview',{method:'POST',body,signal}),
  operationStart: (body,signal) => request('/data/operations/start',{method:'POST',body,signal}),
  dataSettings: signal => request('/data/settings',{signal}),
  dataSettingsPreview: (values,signal) => request('/data/settings/preview',{method:'POST',body:{values},signal}),
  dataSettingsApply: (body,signal) => request('/data/settings/apply',{method:'POST',body,signal}),
  dataSchedules: signal => request('/data/schedules',{signal}),
  dataSchedulePreview: (body,signal) => request('/data/schedules/preview',{method:'POST',body,signal}),
  dataScheduleApply: (body,signal) => request('/data/schedules/apply',{method:'POST',body,signal}),
  dataBackups: (values,signal) => request(`/data/backups${queryString(values)}`,{signal}),
  dataStorage: signal => request('/data/storage',{signal}),
  storageReview: (body,signal) => request('/data/storage/reviews',{method:'POST',body,signal}),
  storageConfirm: (body,signal) => request('/data/storage/confirm',{method:'POST',body,signal}),
  storageJob: (id,signal) => request(`/data/storage/jobs/${encodeURIComponent(id)}`,{signal})
};
