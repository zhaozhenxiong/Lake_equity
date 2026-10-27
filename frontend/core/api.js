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
  health: signal => request('/health',{signal}),
  studies: signal => request('/learning/studies',{signal}),
  study: (id,signal) => request(`/learning/studies/${encodeURIComponent(id)}?view=dashboard`,{signal}),
  studyPreflight: (body,signal) => request('/learning/studies/preflight',{method:'POST',body,signal}),
  releases: signal => request('/strategy/releases',{signal}),
  activeRelease: signal => request('/strategy/active',{signal}),
  releaseStudy: (id,signal) => request(`/strategy/from-study/${encodeURIComponent(id)}`,{method:'POST',body:{},signal}),
  activateRelease: (id,body,signal) => request(`/strategy/releases/${encodeURIComponent(id)}/activate`,{method:'POST',body,signal}),
  fusionRuns: signal => request('/fusion/runs',{signal}),
  fusionRun: (id,signal) => request(`/fusion/runs/${encodeURIComponent(id)}`,{signal}),
  tradingSelections: signal => request('/trading/selections',{signal}),
  planPreflight: (body,signal) => request('/trading/plans/preflight',{method:'POST',body,signal}),
  scanDefaults: signal => request('/scans/defaults',{signal}),
  scanDecisions: (id,values={},signal) => request(`/scans/${encodeURIComponent(id)}/decisions${queryString(values)}`,{signal}),
  scanExplanation: (id,symbol,signal) => request(`/scans/${encodeURIComponent(id)}/explanations/${encodeURIComponent(symbol)}`,{signal}),
  tradingAccounts: signal => request('/trading/accounts',{signal}),
  tradingTemplate: signal => request('/trading/template',{signal}),
  tradingPlans: signal => request('/trading/plans',{signal}),
  tradingAccount: (id,signal) => request(`/trading/accounts/${encodeURIComponent(id)}`,{signal}),
  createTradingAccount: (body,signal) => request('/trading/accounts',{method:'POST',body,signal}),
  saveTradingSnapshot: (id,body,signal) => request(`/trading/accounts/${encodeURIComponent(id)}/snapshots`,{method:'POST',body,signal}),
  tradingPlan: (id,signal) => request(`/trading/plans/${encodeURIComponent(id)}`,{signal}),
  recordTradingFills: (id,body,signal) => request(`/trading/plans/${encodeURIComponent(id)}/fills`,{method:'POST',body,signal}),
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
  jobs: (signal,values={}) => request(`/jobs${queryString(values)}`, {signal}),
  job: (id, signal) => request(`/jobs/${encodeURIComponent(id)}`, {signal}),
  submit: (kind, params, signal) => request('/jobs', {method: 'POST', body: {kind, params}, signal}),
  cancel: (id, signal) => request(`/jobs/${encodeURIComponent(id)}/cancel`, {method: 'POST', body: {}, signal}),
  reports: (signal,values={}) => request(`/reports${queryString(values)}`, {signal}),
  report: (id, signal) => request(`/reports/${encodeURIComponent(id)}?view=dashboard`, {signal}),
  watchlist: signal => request('/watchlist', {signal}),
  accounts: signal => request('/accounts',{signal}),
  accountChartOptions: (values,signal) => request(`/accounts/chart-options${queryString(values)}`,{signal}),
  accountChart: (id,values,signal) => request(`/accounts/${encodeURIComponent(id)}/chart${queryString(values)}`,{signal}),
  addWatch: (symbol, signal) => request('/watchlist', {method: 'POST', body: {symbol}, signal}),
  deleteWatch: (symbol, signal) => request(`/watchlist/${encodeURIComponent(symbol)}`, {method: 'DELETE', signal}),
  // models(signal) keeps the original contract; the second argument opts into the archive view.
  models: (signal, {includeArchived}={}) => request(`/models${includeArchived===undefined?'':queryString({include_archived:String(includeArchived)})}`, {signal}),
  model: (id, signal) => request(`/models/${encodeURIComponent(id)}`, {signal}),
  // One-button research pipeline. Missing endpoints are handled by each page, never by inventing data.
  pipelineLatest: signal => request('/pipeline/latest', {signal}),
  pipelinePreview: (values={}, signal) => request(`/pipeline/preview${queryString(values)}`, {signal}),
  pipeline: (id, signal) => request(`/pipeline/${encodeURIComponent(id)}`, {signal}),
  coverage: (values={}, signal) => request(`/coverage${queryString(values)}`, {signal}),
  archivePreview: signal => request('/models/archive/preview', {method:'POST', body:{}, signal}),
  archiveConfirm: (planId, signal) => request('/models/archive/confirm', {method:'POST', body:{plan_id:planId}, signal}),
  learningPreflight: (body, signal) => request('/learning/preflight', {method:'POST',body,signal}),
  learningSeries: (id, signal) => request(`/learning/series/${encodeURIComponent(id)}`, {signal}),
  // Forward ledger is append-only research evidence; HTTP is read-only.
  forwardSummary: signal => request('/forward/summary', {signal}),
  forwardDays: (limit, signal) => request(`/forward/days${queryString({limit})}`, {signal}),
  replayPreview: (body,signal) => request('/kronos/replays/preview',{method:'POST',body,signal}),
  replayRuns: (signal,values={}) => request(`/kronos/replays${queryString(values)}`,{signal}),
  replay: (id,signal) => request(`/kronos/replays/${encodeURIComponent(id)}`,{signal}),
  // Read-only effective-pool preview; creates no run directory and submits no job.
  backtestPreview: (body,signal) => request('/backtest/preview',{method:'POST',body,signal}),
  // Controlled action: build and save a next-session research plan from a completed backtest.
  backtestPlan: (runId,signal) => request(`/backtests/${encodeURIComponent(runId)}/plan`,{method:'POST',body:{confirm:true},signal}),
  // Failed-build directories: read-only preview, then an explicitly confirmed cleanup.
  quarantinePreview: signal => request('/learning/workflows/quarantine',{signal}),
  quarantineConfirm: (body,signal) => request('/learning/workflows/quarantine/confirm',{method:'POST',body,signal}),
  learningCatalog: signal => request('/learning/catalog', {signal}),
  learningRuns: signal => request('/learning/runs', {signal}),
  learningRun: (id, signal) => request(`/learning/runs/${encodeURIComponent(id)}?view=dashboard`, {signal}),
  predictionChart: (reference, signal, {predictionRunId}={}) => request(`/predictions/${encodeURIComponent(reference)}/chart${queryString({prediction_run_id:predictionRunId})}`, {signal}),
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
  learningRetentionCandidates: signal => request('/data/learning-retention/candidates',{signal}),
  learningRetentionReview: (body,signal) => request('/data/learning-retention/reviews',{method:'POST',body,signal}),
  learningRetentionConfirm: (body,signal) => request('/data/learning-retention/confirm',{method:'POST',body,signal}),
  learningRetentionPlan: (id,signal) => request(`/data/learning-retention/plans/${encodeURIComponent(id)}`,{signal}),
  learningRetentionCancel: (id,signal) => request(`/data/learning-retention/plans/${encodeURIComponent(id)}/cancel`,{method:'POST',body:{},signal}),
  learningRetentionReconcile: (id,body,signal) => request(`/data/learning-retention/plans/${encodeURIComponent(id)}/reconcile`,{method:'POST',body,signal}),
  storageUsage: (values,signal) => request(`/data/storage/usage${queryString(values)}`,{signal}),
  refreshStorageUsage: (body,signal) => request('/data/storage/usage/refresh',{method:'POST',body,signal}),
  storageReview: (body,signal) => request('/data/storage/reviews',{method:'POST',body,signal}),
  storageConfirm: (body,signal) => request('/data/storage/confirm',{method:'POST',body,signal}),
  storageJob: (id,signal) => request(`/data/storage/jobs/${encodeURIComponent(id)}`,{signal})
};
