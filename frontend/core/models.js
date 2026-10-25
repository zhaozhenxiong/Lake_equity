import {api} from './api.js';
import {arrayOf,badge,disclosure,empty,errorHtml,escapeHtml,number,percent,routeLink} from './ui.js';

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
const qualificationLabels={research_only:'研究用途',paper_allocation_eligible:'模拟配置资格',strict_history_eligible:'严格历史资格',independent_forward_status:'独立前瞻'};
const qualificationText=value=>({research_default_candidate:'研究默认候选',research_default:'研究默认',provisional_research_active:'研究默认（临时启用）',candidate:'候选',pending:'待前瞻',passed:'通过',eligible:'已授予',not_eligible:'未授予',false:'未授予',true:'已授予'})[String(value)] || String(value);

// One neutral shape for both `overview.default_method` and `strategy/active.release`.
export function normalizeMethod(method, release){
  const source=method || release;
  if(!source || typeof source!=='object')return null;
  const protocol=source.protocol || {}, risk=source.risk || {}, qualification=source.qualification || {};
  return {
    release_id:source.release_id || '', name:source.name || '阶段研究主模型', model_id:source.model_id || '',
    series_id:source.series_id || '', information_cutoff:source.information_cutoff || '',
    model_name:protocol.model_name || source.model_name || source.model_type || '', model_type:protocol.model_type || source.model_type || '',
    protocol_id:protocol.protocol_id || '', train_sessions:protocol.train_sessions, protocol, risk, qualification
  };
}

export function methodQualificationChips(qualification={}){
  const chips=[];
  if(qualification.stage)chips.push(`<span class="status-chip neutral">${escapeHtml(qualificationText(qualification.stage))}</span>`);
  if(qualification.research_only===true)chips.push('<span class="status-chip warn">研究用途</span>');
  chips.push(qualification.paper_allocation_eligible===true?'<span class="status-chip warn">模拟配置资格已授予</span>':'<span class="status-chip neutral">模拟配置资格未授予</span>');
  chips.push(qualification.strict_history_eligible===true?'<span class="status-chip good">严格历史资格通过</span>':'<span class="status-chip neutral">严格历史资格未通过</span>');
  return chips.join('');
}

const defaultMethodStageText=value=>({research_default:'研究默认',research_default_candidate:'研究默认候选',provisional_research_active:'研究默认（临时启用）',candidate:'候选',pending:'待前瞻'})[String(value)] || '';
const defaultMethodActivationText=source=>{
  const value=source?.activation ?? source?.activation_note ?? source?.active_note;
  if(typeof value==='string')return value;
  if(value && typeof value==='object')return value.text || value.reason || value.note || '';
  return '';
};
const defaultMethodSeriesInterval=source=>{
  const series=source?.series && typeof source.series==='object'?source.series:{};
  return {
    series_id:source?.series_id || series.series_id || source?.model_series_id || '',
    start:series.start || series.start_session || series.first_session || source?.series_start || '',
    end:series.end || series.end_session || series.last_session || source?.series_end || ''
  };
};

// Reusable facts block for the default method: release identity, protocol, cutoff, series interval,
// one-sentence reason why it is the default, and the model-centre link to change it. Pure: no network.
export function defaultMethodExplanation(model, release){
  const source=release && typeof release==='object'?release:(model && typeof model==='object'?model:null);
  if(!source)return empty('尚无默认方法','启用一个阶段研究版本后，这里会说明它为什么是当前默认，以及如何更换。',`<a class="button" href="#models">到模型中心启用</a>`);
  const view=normalizeMethod(source,source);
  const detail=model && typeof model==='object' && model!==source?model:{};
  const protocol=source.protocol && typeof source.protocol==='object'?source.protocol:{};
  const qualification=view.qualification || {};
  const modelType=view.model_type || detail.model_type || '';
  const featureCount=detail.feature_count ?? source.feature_count ?? protocol.feature_count;
  const trainSessions=view.train_sessions ?? detail.train_sessions ?? protocol.train_sessions;
  const cutoff=view.information_cutoff || detail.information_cutoff || '';
  const stage=defaultMethodStageText(qualification.stage) || (qualification.research_only===true?'研究默认 · 待前瞻':'');
  const activation=defaultMethodActivationText(source);
  const interval=defaultMethodSeriesInterval(source);
  const seriesText=interval.series_id?`${escapeHtml(interval.series_id)}${interval.start||interval.end?` · ${escapeHtml(interval.start || '—')} 至 ${escapeHtml(interval.end || '—')}`:''}`:'';
  const facts=[
    `<div><dt>默认版本</dt><dd><strong>${escapeHtml(view.name || '阶段研究主模型')}</strong><br><span class="code-key">${escapeHtml(view.release_id || '版本未返回')}</span></dd></div>`,
    `<div><dt>协议模型</dt><dd>${escapeHtml(modelName(view.model_name || modelType || ''))}${modelType?` · <span class="code-key">${escapeHtml(modelType)}</span>`:''}${view.protocol_id?` · <span class="code-key">${escapeHtml(view.protocol_id)}</span>`:''}</dd></div>`,
    `<div><dt>特征维数 / 训练窗口</dt><dd>${featureCount===undefined||featureCount===null?'—':`${number(featureCount,0)} 项`} · ${trainSessions===undefined||trainSessions===null?'—':`${number(trainSessions,0)} 交易日`}</dd></div>`,
    `<div><dt>信息截止</dt><dd class="code-key">${escapeHtml(cutoff || '—')}</dd></div>`
  ];
  if(seriesText)facts.push(`<div><dt>原序列区间</dt><dd class="code-key">${seriesText}</dd></div>`);
  if(stage || activation)facts.push(`<div><dt>激活与资格</dt><dd>${escapeHtml(stage || '已启用')}${activation?` · ${escapeHtml(activation)}`:''}<span class="method-chips">${methodQualificationChips(qualification)}</span></dd></div>`);
  const why=`它是模型中心当前启用的阶段研究版本，扫描、固定模型回测和关注页入口都只使用这一版方法${cutoff?`（信息截止 ${cutoff}）`:''}；要更换默认方法需在模型中心重新启用其他版本。`;
  return `<div class="method-explanation" data-default-method-explanation><h3>为什么这是默认方法</h3><dl class="method-facts">${facts.join('')}</dl><p class="form-note" data-default-method-why>${escapeHtml(why)}</p><div class="button-row space-top"><a class="small" href="#models">到模型中心查看或更换默认方法</a></div></div>`;
}

// Identity block shared by the overview hero and the model centre default-method card.
export function defaultMethodIdentity(method, release, {compact=false, showLink=true}={}){
  const view=normalizeMethod(method, release);
  if(!view)return empty('尚无默认方法','运行一次流水线或启用一个阶段研究版本，之后默认只用这一个方法。',`<a class="button primary" href="#models">到模型中心启用</a>`);
  const risk=view.risk || {}, qualification=view.qualification || {};
  const facts=compact?'':`<dl class="method-facts"><div><dt>模型</dt><dd>${escapeHtml(modelName(view.model_name || view.model_type))} <span class="method-code">${escapeHtml(view.model_id.slice(0,18) || '—')}</span></dd></div><div><dt>训练协议</dt><dd class="method-code">${escapeHtml(view.protocol_id || '—')}${view.train_sessions?` · ${number(view.train_sessions,0)} 交易日`:''}</dd></div><div><dt>信息截止</dt><dd class="method-code">${escapeHtml(view.information_cutoff || '—')}</dd></div><div><dt>风险上限</dt><dd>总仓 ${percent(risk.max_total_weight)} · 单票 ${percent(risk.max_stock_weight)} · Top ${number(risk.top_k,0)} · ${number(risk.rebalance_sessions,0)} 日调仓</dd></div></dl>`;
  return `<div class="method-identity"><div class="method-top"><span class="method-eyebrow">默认方法</span>${qualification.research_only===true?badge('partial','研究默认 · 待前瞻'):''}</div><h2 class="method-name">${escapeHtml(view.name)}</h2><p class="method-release code-key">${escapeHtml(view.release_id || '版本未返回')}</p>${facts}<div class="method-chips">${methodQualificationChips(qualification)}</div>${showLink?'<div class="method-links"><a class="small" href="#models">查看方法来源</a><a class="small" href="#backtest">回测口径</a></div>':''}</div>`;
}

// The system default frozen model is the active strategy release; no manual selection on consumer pages.
export function defaultModelIdentity(response){
  const active=response?.research_active;
  const release=active && typeof active==='object' && !Array.isArray(active)?active:null;
  if(!release)return {release:null,model:null,modelId:'',source:'no_default_method',detailMissing:false};
  const modelId=release.model_id || '';
  const model=arrayOf(response,'models').find(row=>row?.model_id===modelId) || null;
  return {release,model,modelId,source:'active_release',detailMissing:!model};
}

export async function mountDefaultModel(host,{signal,onChange}={}){
  const target=typeof host==='string'?(typeof document==='undefined'?null:document.querySelector(host)):host;
  if(!target)return null;
  let identity;
  try{
    identity=defaultModelIdentity(await api.models(signal));
    if(signal?.aborted)return null;
    if(identity.release && !identity.model && identity.modelId){
      try{const detailed=await api.model(identity.modelId,signal);if(signal?.aborted)return null;identity.model=detailed && typeof detailed==='object'?detailed:null;identity.detailMissing=!identity.model;}
      catch(error){if(error?.name==='AbortError'||signal?.aborted)return null;identity.detailMissing=true;}
    }
  }catch(error){if(error?.name==='AbortError'||signal?.aborted)return null;target.innerHTML=errorHtml(error);return null;}
  if(signal?.aborted)return null;
  if(identity.release){
    const cutoff=identity.release.information_cutoff || identity.model?.information_cutoff || '';
    target.innerHTML=`${identity.model?modelSummary(identity.model):empty('默认冻结模型详情不可用','默认方法引用的模型版本可能已归档；请到模型中心重新启用默认方法。',`<a class="button small-button" href="#models">到模型中心</a>`)}<p class="form-note" data-default-model-note>系统自动使用默认方法，这里不提供手动选择；如需更换请到 <a href="#models">模型中心</a>。发布版本 ${escapeHtml(identity.release.release_id || '默认版本')}${cutoff?`，信息截止 ${escapeHtml(cutoff)}`:''}。</p>`;
  }else{
    target.innerHTML=`${empty('尚未启用默认方法','扫描和固定模型回测只会使用系统默认方法；请先在模型中心启用一个阶段研究版本。',`<a class="button primary" href="#models">到模型中心启用</a>`)}<p class="form-note">默认方法未启用时，关注页的扫描与回测入口不会提交。</p>`;
  }
  onChange?.(identity);
  return identity;
}

export async function bindModelSelector(host,{signal,selected='',onChange,includeArchived}={}){
  const response=await api.models(signal,{includeArchived});if(signal?.aborted)return {models:[],series:[]};
  const models=arrayOf(response,'models'),series=arrayOf(response,'series');
  const select=host.querySelector('[data-model-select]'),detail=host.querySelector('[data-model-identity]');
  select.innerHTML=modelOptions(models,selected);
  function update(){const model=models.find(row=>row.model_id===select.value);if(detail)detail.innerHTML=modelSummary(model);onChange?.(model);}
  select.addEventListener('change',update,signal?{signal}:{});update();
  return {models,series,response};
}
export function modelLinks(model){return `<a class="button small-button" href="${escapeHtml(routeLink('scan',{model_id:model.model_id}))}">用于选股</a><a class="button small-button" href="${escapeHtml(routeLink('backtest',{model_id:model.model_id,mode:'frozen'}))}">固定模型回测</a>${seriesId(model)?`<a class="button small-button" href="${escapeHtml(routeLink('backtest',{series_id:seriesId(model),mode:'rolling'}))}">滚动回测</a>`:''}`;}
