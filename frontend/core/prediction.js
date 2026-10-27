import {day} from './ui.js';

export const predictionReference=value=>value?.result_ref?.cache_key || value?.prediction_ref || value?.prediction_reference?.cache_key || '';

export function predictionQuality(value,{chart=null,error=null}={}){
  if(error)return {state:error.status===404?'unavailable':'failed',label:error.status===404?'不可用':'失败',reason:error.message || '预测读取失败'};
  if(value?.status==='failed')return {state:'failed',label:'失败',reason:value.error || value.reason || '预测执行失败'};
  const quality=value?.quality || chart?.quality || {},forecast=value?.forecast || chart?.forecast || [];
  if(!forecast.length || typeof quality.prediction_usable!=='boolean')return {state:'unavailable',label:'不可用',reason:!forecast.length?'没有可读预测路径':'预测质量字段缺失'};
  const verified=chart?.history_verification?.status==='verified' && chart?.history_verification?.input_sha256_match===true;
  const reasons=[];
  if(quality.prediction_usable!==true){
    if(quality.valid_samples!==undefined)reasons.push(`有效采样 ${quality.valid_samples}/${quality.requested_samples ?? '—'}`);
    for(const [key,label] of [['calendar_verified','交易日历未核验'],['input_contract_verified','输入合同未核验'],['source_verified','来源未核验']])if(quality[key]===false)reasons.push(label);
    reasons.push(quality.reason || '预测未通过原质量门槛，仅作采样情景');
  }
  if(!verified)reasons.push(chart?.history_verification?.reason || '原预测历史尚未核验，不能拼接当前行情');
  if(chart?.history_verification?.display_only_degraded===true)reasons.push('历史由分析绑定核验；保持降级参考，不提升原预测资格');
  return reasons.length?{state:'degraded',label:'降级参考',reason:reasons.join('；')}:{state:'usable',label:'可用',reason:'研究预测；采样比例不是校准概率'};
}

export function analysisPredictionCompatible(analysis,result,chart){
  const fail=(code,reason)=>({compatible:false,code,reason});
  const binding=result?.analysis_binding;
  if(binding?.compatible!==true)return fail('analysis_binding_missing','预测没有可核验的分析绑定，未叠加当前结构图');
  const id=analysis?.lake_run_id || analysis?.run_id,cutoff=day(analysis?.as_of || analysis?.data_end);
  if(!id || binding.analysis_run_id!==id)return fail('analysis_changed','预测属于另一份结构分析');
  if(!analysis.input_manifest_id || binding.input_manifest_id!==analysis.input_manifest_id)return fail('manifest_changed','分析与预测输入代际不一致');
  if(binding.symbol!==analysis.symbol || result.symbol!==analysis.symbol || chart?.symbol!==analysis.symbol)return fail('symbol_mismatch','分析与预测股票不一致');
  if(binding.as_of!==cutoff || result.as_of!==cutoff || chart?.as_of!==cutoff || analysis.data_end && day(analysis.data_end)!==cutoff)return fail('cutoff_mismatch','分析与预测行情截止不一致');
  if(binding.price_basis!==analysis.price_basis || chart?.input?.price_basis!==binding.price_basis)return fail('price_basis_mismatch','分析与预测价格口径不一致');
  if(chart?.history_verification?.status!=='verified' || chart.history_verification.input_sha256_match!==true)return fail('history_unavailable',chart?.history_verification?.reason || '原预测历史未核验，不能拼接');
  if(!/^[a-f0-9]{64}$/.test(binding.input_window_sha256 || '') || !/^[a-f0-9]{64}$/.test(binding.history_input_sha256 || '') || chart.input?.data_sha256!==binding.history_input_sha256)return fail('window_hash_mismatch','预测输入窗口身份不一致');
  const lookback=binding.lookback,bars=analysis.frequencies?.['日线']?.bars || analysis.bars || [],history=chart.history || [];
  if(!Number.isInteger(lookback) || lookback<1 || bars.length<lookback || history.length!==lookback)return fail('history_window_missing','原分析未保留完整预测输入窗口');
  const saved=bars.slice(-lookback);
  if(day(saved.at(-1)?.time || saved.at(-1)?.date)!==cutoff || saved.at(-1)?.is_closed===false)return fail('cutoff_bar_missing','分析截止日不是已收盘的实际日线');
  for(let index=0;index<lookback;index++){
    const left=saved[index],right=history[index];
    if(day(left.time || left.date)!==day(right.time || right.date) || ['open','high','low','close','volume'].some(key=>!Number.isFinite(Number(left[key] ?? (key==='volume'?left.vol:undefined))) || Number(left[key] ?? left.vol)!==Number(right[key])))return fail('history_window_changed','预测历史与当前结构图尾部输入不一致');
  }
  const horizon=Number(result.parameters?.horizon ?? result.identity?.parameters?.horizon ?? chart.parameters?.horizon);
  if(![1,5].includes(horizon) || chart.forecast?.length!==horizon)return fail('horizon_mismatch','预测交易日数与分析页请求不一致');
  if(chart.forecast.some((row,index)=>{const time=day(row.date || row.time),middle=Number(row.close ?? row.close_median ?? row.p50),low=Number(row.close_p10 ?? row.p10),high=Number(row.close_p90 ?? row.p90);return !/^\d{4}-\d{2}-\d{2}$/.test(time)||time<=cutoff||index>0&&time<=day(chart.forecast[index-1].date || chart.forecast[index-1].time)||![middle,low,high].every(value=>Number.isFinite(value)&&value>0)||low>middle||middle>high;}))return fail('forecast_invalid','预测日期或采样分位带无效');
  return {compatible:true,code:'matched',reason:''};
}
