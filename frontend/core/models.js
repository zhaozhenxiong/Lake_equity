import {api} from './api.js';
import {arrayOf,disclosure,empty,escapeHtml,number,routeLink} from './ui.js';

export const modelName=value=>({ridge:'Ridge',lightgbm:'LightGBM',mlp:'均线多因子 MLP',legacy_ma_ridge:'均线特征 · Ridge 基线',legacy_ma_mlp:'均线特征 · MLP',price_volume_ridge:'纯量价 38 项 · Ridge',price_volume_lightgbm:'纯量价 38 项 · LightGBM',price_volume_mlp:'纯量价 38 项 · MLP',factor_ridge:'多因子 · Ridge',factor_lightgbm:'多因子 · LightGBM',factor_mlp:'多因子 · MLP'})[value] || value || '模型';
export const seriesId=value=>value?.series_id || value?.model_series_id || '';
export function modelOptions(models,selected=''){
  return '<option value="">请选择已冻结的模型版本</option>'+models.map(model=>`<option value="${escapeHtml(model.model_id)}"${selected===model.model_id?' selected':''}>${escapeHtml(modelName(model.model_name || model.model_type))} · ${number(model.feature_count,0)} 维 · ${escapeHtml(model.information_cutoff || '截止未返回')} · ${escapeHtml(model.model_id)}</option>`).join('');
}
export function seriesOptions(series,selected=''){
  return '<option value="">请选择已保存的滚动序列</option>'+series.map(row=>`<option value="${escapeHtml(seriesId(row))}"${selected===seriesId(row)?' selected':''}>${escapeHtml(modelName(row.model_name || row.model_type || seriesId(row)))} · ${escapeHtml(seriesId(row))}</option>`).join('');
}
export function modelSummary(model){
  if(!model)return empty('尚未选择模型版本','先在模型中心训练，再明确选择用于扫描和回测的冻结版本。');
  return `<dl class="detail-list"><div><dt>模型版本</dt><dd class="code-key">${escapeHtml(model.model_id)} / ${escapeHtml(model.version || model.model_version || '—')}</dd></div><div><dt>输入维度</dt><dd>${number(model.feature_count,0)} 项</dd></div><div><dt>信息截止 / 冻结时间</dt><dd>${escapeHtml(model.information_cutoff || '—')} / ${escapeHtml(model.frozen_at || '—')}</dd></div>${[model.usage,model.validation_usage].includes('frozen_inference_only')?'<div><dt>验证边界</dt><dd>最终重拟合版本，没有独立样本外成绩</dd></div>':''}<div><dt>资格</dt><dd>研究候选；${model.qualification?.strict_history_eligible===true?'严格历史资格通过':'严格历史资格未授予'}</dd></div></dl>${disclosure('冻结模型完整身份',model)}`;
}
export async function bindModelSelector(host,{signal,selected='',onChange}={}){
  const response=await api.models(signal);if(signal?.aborted)return {models:[],series:[]};
  const models=arrayOf(response,'models'),series=arrayOf(response,'series');
  const select=host.querySelector('[data-model-select]'),detail=host.querySelector('[data-model-identity]');
  select.innerHTML=modelOptions(models,selected);
  function update(){const model=models.find(row=>row.model_id===select.value);if(detail)detail.innerHTML=modelSummary(model);onChange?.(model);}
  select.addEventListener('change',update,signal?{signal}:{});update();
  return {models,series,response};
}
export function modelLinks(model){return `<a class="button small-button" href="${escapeHtml(routeLink('scan',{model_id:model.model_id}))}">用于选股</a><a class="button small-button" href="${escapeHtml(routeLink('backtest',{model_id:model.model_id,mode:'frozen'}))}">固定模型回测</a>${seriesId(model)?`<a class="button small-button" href="${escapeHtml(routeLink('backtest',{series_id:seriesId(model),mode:'rolling'}))}">滚动回测</a>`:''}`;}
